import gzip
import hashlib
import json
import random
import tempfile
from datetime import datetime
from io import BytesIO
from typing import cast
from urllib.parse import quote_plus, unquote_plus
from uuid import uuid4

import pytest
from mesh_client import MeshClient
from mypy_boto3_events import EventBridgeClient
from mypy_boto3_events.type_defs import PutEventsRequestEntryTypeDef
from mypy_boto3_lambda import LambdaClient
from mypy_boto3_s3.service_resource import Bucket
from mypy_boto3_ssm import SSMClient
from mypy_boto3_stepfunctions import SFNClient

from integration.constants import (
    FETCH_FUNCTION,
    FETCH_LOG_GROUP,
    GET_MESSAGES_SFN_ARN,
    LOCAL_MAILBOXES,
    MB,
    POLL_FUNCTION,
    POLL_LOG_GROUP,
    SEND_LOG_GROUP,
)
from integration.test_helpers import (
    CloudwatchLogsCapture,
    sync_json_lambda_invocation_successful,
    temp_mapping_for_s3_object,
    wait_for,
    wait_for_execution_outcome,
    wait_till_not_running,
)


def sample_trigger_event(bucket: str, key: str) -> PutEventsRequestEntryTypeDef:
    key = (key or "").strip().lstrip("/")
    assert bucket
    assert key

    return PutEventsRequestEntryTypeDef(
        Time=datetime.utcnow(),
        Source="aws.s3",
        DetailType="AWS API Call via CloudTrail",
        Detail=json.dumps(
            {
                "eventTime": datetime.utcnow().isoformat(),
                "eventSource": "s3.amazonaws.com",
                "eventName": "PutObject",
                "awsRegion": "eu-west-2",
                "requestParameters": {
                    "bucketName": bucket,
                    "Host": f"{bucket}.s3.eu-west-2.amazonaws.com",
                    "key": key,
                },
                "eventID": uuid4().hex,
                "readOnly": False,
                "resources": [
                    {
                        "type": "AWS::S3::Object",
                        "ARN": f"arn:aws:s3:::{bucket}/{key}",
                    },
                    {
                        "accountId": "000000000000",
                        "type": "AWS::S3::Bucket",
                        "ARN": f"arn:aws:s3:::{bucket}",
                    },
                ],
                "eventType": "AwsApiCall",
                "managementEvent": False,
                "recipientAccountId": "000000000000",
                "eventCategory": "Data",
            }
        ),
    )


def test_send_receive_with_metadata(
    local_mesh_bucket: Bucket,
    sfn: SFNClient,
    events: EventBridgeClient,
    ssm: SSMClient,
    lambdas: LambdaClient,
    mesh_client_two: MeshClient,
):
    wait_till_not_running(state_machine_arn=GET_MESSAGES_SFN_ARN, sfn=sfn)

    sender = LOCAL_MAILBOXES[0]
    recipient = LOCAL_MAILBOXES[1]
    filename = f"{uuid4().hex}.dat"
    content = b"test"
    workflow_id = f"{uuid4().hex[:8]}_{uuid4().hex[:8]}"

    mex_filename = "ooo/&*:/.txt"
    subject = f"aa - $$ $%% - {uuid4().hex}"
    local_id = f"$lid$-{uuid4().hex}"
    partner_id = f"$pid$-{uuid4().hex}"
    checksum = uuid4().hex

    key = f"outbound/{filename}"
    s3_object = local_mesh_bucket.Object(key)

    content_type = "text/plain"

    with CloudwatchLogsCapture(log_group=SEND_LOG_GROUP) as cw:
        s3_object.put(
            Body=content,
            ContentType=content_type,
            Metadata={
                "mex-from": sender,
                "mex-to": recipient,
                "mex-filename": quote_plus(mex_filename),
                "mex-workflowid": quote_plus(workflow_id),
                "mex-subject": quote_plus(subject),
                "mex-localid": quote_plus(local_id),
                "mex-partnerid": quote_plus(partner_id),
                "mex-content-checksum": quote_plus(checksum),
            },
        )

        events.put_events(
            Entries=[sample_trigger_event(local_mesh_bucket.name, key)]
        )  # no cloudtrail in localstack
        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )

    wait_for(lambda: len(mesh_client_two.list_messages()) > 0)

    messages = mesh_client_two.list_messages()
    assert len(messages) == 1
    message_id = messages[0]

    message = mesh_client_two.retrieve_message(message_id)
    assert message.message_id == message_id
    assert message.read() == content
    assert message.sender == sender
    assert message.recipient == recipient
    assert message.filename == mex_filename
    assert message.workflow_id == workflow_id
    assert message.subject == subject
    assert message.local_id == local_id
    assert message.partner_id == partner_id
    assert message.checksum == checksum  # type: ignore[attr-defined]

    with CloudwatchLogsCapture(log_group=POLL_LOG_GROUP) as cw:
        response = lambdas.invoke(
            FunctionName=POLL_FUNCTION,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=json.dumps({"mailbox": recipient}).encode("utf-8"),
        )

        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )

    response_payload, _ = sync_json_lambda_invocation_successful(response)
    assert response_payload
    assert response_payload.get("statusCode") == 200
    body = response_payload.get("body")
    assert body
    assert body["message_count"] == 1

    received = body["message_list"][0]["body"]
    assert received["message_id"] == message_id

    with CloudwatchLogsCapture(log_group=FETCH_LOG_GROUP) as cw:
        response = lambdas.invoke(
            FunctionName=FETCH_FUNCTION,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=json.dumps(
                {"body": {"message_id": message_id, "dest_mailbox": recipient}}
            ).encode("utf-8"),
        )

        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )
        logs = cw.find_logs(parse_logs=True)

    assert logs
    assert all(
        log.get("Log_Level") == "INFO" for log in logs if log and "Log_Level" in log
    ), logs

    payload, _ = sync_json_lambda_invocation_successful(response)
    assert payload
    assert payload.get("statusCode") == 200
    body = payload.get("body")
    assert body
    assert body["complete"]
    assert body["s3_bucket"] == local_mesh_bucket.name
    assert body["s3_key"] == f"inbound/{recipient}/{message_id}.dat"

    received = local_mesh_bucket.Object(body["s3_key"])
    get_resp = received.get()
    assert get_resp["Body"].read() == content
    metadata = received.metadata
    assert metadata.get("mex-from") == sender
    assert metadata.get("mex-to") == recipient
    assert unquote_plus(metadata.get("mex-filename")) == mex_filename
    assert unquote_plus(metadata.get("mex-workflowid")) == workflow_id
    assert unquote_plus(metadata.get("mex-subject")) == subject
    assert unquote_plus(metadata.get("mex-localid")) == local_id
    assert unquote_plus(metadata.get("mex-partnerid")) == partner_id
    assert unquote_plus(metadata.get("mex-content-checksum")) == checksum

    message = mesh_client_two.retrieve_message(message_id)
    message.status = "acknowledged"  # type: ignore[attr-defined]


def test_send_receive_with_metadata_all_settings(
    local_mesh_bucket: Bucket,
    sfn: SFNClient,
    events: EventBridgeClient,
    ssm: SSMClient,
    lambdas: LambdaClient,
    mesh_client_two: MeshClient,
):
    wait_till_not_running(state_machine_arn=GET_MESSAGES_SFN_ARN, sfn=sfn)

    sender = LOCAL_MAILBOXES[0]
    recipient = LOCAL_MAILBOXES[1]
    filename = f"{uuid4().hex}.dat"
    content = b"test"
    workflow_id = f"{uuid4().hex[:8]}_{uuid4().hex[:8]}"

    mex_filename = "ooo/&*:/.txt"
    subject = f"aa - $$ $%% - {uuid4().hex}"
    local_id = f"$lid$-{uuid4().hex}"
    partner_id = f"$pid$-{uuid4().hex}"
    checksum = uuid4().hex

    key = f"outbound/{filename}"
    s3_object = local_mesh_bucket.Object(key)

    content_type = "text/plain"

    with CloudwatchLogsCapture(log_group=SEND_LOG_GROUP) as cw:
        s3_object.put(
            Body=content,
            ContentType=content_type,
            Metadata={
                "mex-from": sender,
                "mex-to": recipient,
                "mex-filename": quote_plus(mex_filename),
                "mex-workflowid": quote_plus(workflow_id),
                "mex-subject": quote_plus(subject),
                "mex-localid": quote_plus(local_id),
                "mex-partnerid": quote_plus(partner_id),
                "mex-content-checksum": quote_plus(checksum),
                # just testing all the settings ( not sensible settings )
                "mex-content-compress": "y",
                "mex-content-compressed": "y",
                "mex-content-encrypted": "y",
            },
        )

        events.put_events(
            Entries=[sample_trigger_event(local_mesh_bucket.name, key)]
        )  # no cloudtrail in localstack
        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )

    wait_for(lambda: len(mesh_client_two.list_messages()) > 0)
    messages = mesh_client_two.list_messages()
    assert len(messages) == 1
    message_id = messages[0]

    message = mesh_client_two.retrieve_message(message_id)
    assert message.message_id == message_id
    assert message.read() == content
    assert message.sender == sender
    assert message.recipient == recipient
    assert message.filename == mex_filename
    assert message.workflow_id == workflow_id
    assert message.subject == subject
    assert message.local_id == local_id
    assert message.partner_id == partner_id
    assert message.checksum == checksum  # type: ignore[attr-defined]
    assert message.compressed
    assert message.encrypted

    with CloudwatchLogsCapture(log_group=POLL_LOG_GROUP) as cw:
        response = lambdas.invoke(
            FunctionName=POLL_FUNCTION,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=json.dumps({"mailbox": recipient}).encode("utf-8"),
        )

        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )

    response_payload, _ = sync_json_lambda_invocation_successful(response)
    assert response_payload
    assert response_payload.get("statusCode") == 200
    body = response_payload.get("body")
    assert body
    assert body["message_count"] == 1

    received = body["message_list"][0]["body"]
    assert received["message_id"] == message_id

    with CloudwatchLogsCapture(log_group=FETCH_LOG_GROUP) as cw:
        response = lambdas.invoke(
            FunctionName=FETCH_FUNCTION,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=json.dumps(
                {"body": {"message_id": message_id, "dest_mailbox": recipient}}
            ).encode("utf-8"),
        )

        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )
        logs = cw.find_logs(parse_logs=True)

    assert logs
    assert all(
        log.get("Log_Level") == "INFO" for log in logs if log and "Log_Level" in log
    ), logs

    payload, _ = sync_json_lambda_invocation_successful(response)
    assert payload
    assert payload.get("statusCode") == 200
    body = payload.get("body")
    assert body
    assert body["complete"]
    assert body["s3_bucket"] == local_mesh_bucket.name
    assert body["s3_key"] == f"inbound/{recipient}/{message_id}.dat"

    received = local_mesh_bucket.Object(body["s3_key"])
    get_resp = received.get()
    assert get_resp["Body"].read() == content
    metadata = received.metadata
    assert metadata.get("mex-from") == sender
    assert metadata.get("mex-to") == recipient
    assert unquote_plus(metadata.get("mex-filename")) == mex_filename
    assert unquote_plus(metadata.get("mex-workflowid")) == workflow_id
    assert unquote_plus(metadata.get("mex-subject")) == subject
    assert unquote_plus(metadata.get("mex-localid")) == local_id
    assert unquote_plus(metadata.get("mex-partnerid")) == partner_id
    assert unquote_plus(metadata.get("mex-content-checksum")) == checksum
    assert unquote_plus(metadata.get("mex-content-encrypted")) == "Y"
    assert unquote_plus(metadata.get("mex-content-compressed")) == "Y"

    message = mesh_client_two.retrieve_message(message_id)
    message.status = "acknowledged"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("size", "content_encoding", "compress", "mex_compress"),
    [
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "",
            False,
            False,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "",
            False,
            True,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "",
            True,
            False,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "",
            True,
            True,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "gzip",
            False,
            False,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "gzip",
            False,
            True,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "gzip",
            True,
            False,
        ),
        (
            random.randint(
                (13 * MB) - random.randint(0, 511), (33 * MB) + random.randint(0, 4)
            ),
            "gzip",
            True,
            True,
        ),
    ],
)
def test_send_receive_large_file(
    local_mesh_bucket: Bucket,
    sfn: SFNClient,
    events: EventBridgeClient,
    ssm: SSMClient,
    mesh_client_two: MeshClient,
    size: int,
    content_encoding: str,
    compress: bool,
    mex_compress: bool,
):
    wait_till_not_running(state_machine_arn=GET_MESSAGES_SFN_ARN, sfn=sfn)

    sender = LOCAL_MAILBOXES[0]
    recipient = LOCAL_MAILBOXES[1]
    filename = f"{uuid4().hex}.dat"

    workflow_id = f"{uuid4().hex[:8]}_{uuid4().hex[:8]}"
    local_id = uuid4().hex

    key = f"outbound/{filename}"
    s3_object = local_mesh_bucket.Object(key)

    metadata = {
        "mex-from": sender,
        "mex-to": recipient,
        "mex-workflowid": quote_plus(workflow_id),
        "mex-localid": quote_plus(local_id),
    }

    if mex_compress:
        metadata["mex-content-compress"] = "y"

    written = 0
    hash_in = hashlib.md5()
    in_start = b""

    with CloudwatchLogsCapture(log_group=SEND_LOG_GROUP) as cw:
        with tempfile.NamedTemporaryFile() as f:
            buffer = f
            if compress:
                buffer = gzip.open(f, mode="wb")  # type: ignore[assignment]

            while written < size:
                block = (
                    random.randbytes(random.randint(8, 14))
                    + (b":abc123" * 20)
                    + random.randbytes(random.randint(8, 14))
                )
                if len(in_start) < 200:
                    in_start += block
                    in_start = in_start[:200]

                block = block[: size - written]
                hash_in.update(block)
                buffer.write(block)
                written += len(block)

            buffer.flush()
            if compress:
                buffer.close()
            f.flush()
            f.seek(0)

            s3_object.put(
                Body=cast(BytesIO, f),
                ContentType="application/octet-stream",
                ContentEncoding=content_encoding,
                Metadata=metadata,
            )

            events.put_events(
                Entries=[sample_trigger_event(local_mesh_bucket.name, key)]
            )  # no cloudtrail in localstack

        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )

    wait_for(lambda: len(mesh_client_two.list_messages()) > 0)
    messages = mesh_client_two.list_messages()
    assert len(messages) == 1
    message_id = messages[0]

    message = mesh_client_two.retrieve_message(message_id)
    assert message.message_id == message_id

    assert message.sender == sender
    assert message.recipient == recipient
    assert message.workflow_id == workflow_id

    hash_mid = hashlib.md5()

    received_bytes = gzip.decompress(message.read()) if compress else message.read()

    hash_mid.update(received_bytes)

    assert hash_mid.hexdigest() == hash_in.hexdigest()

    with CloudwatchLogsCapture(log_group=POLL_LOG_GROUP) as cw:
        execution = sfn.start_execution(
            stateMachineArn=GET_MESSAGES_SFN_ARN,
            name=uuid4().hex,
            input=json.dumps({"mailbox": recipient}),
        )

    output, result = wait_for_execution_outcome(
        execution_arn=execution["executionArn"], sfn=sfn
    )

    assert result["status"] == "SUCCEEDED"
    assert output

    received = local_mesh_bucket.Object(f"inbound/{recipient}/{message_id}.dat")
    metadata = received.metadata
    assert metadata.get("mex-from") == sender
    assert metadata.get("mex-to") == recipient
    assert unquote_plus(metadata.get("mex-workflowid") or "") == workflow_id
    assert unquote_plus(metadata.get("mex-localid") or "") == local_id

    hash_out = hashlib.md5()
    res = received.get()
    out_start = b""

    out_buffer = res["Body"]
    if compress:
        out_buffer = gzip.GzipFile(fileobj=res["Body"])  # type: ignore[assignment]

    while True:
        block = out_buffer.read(1 * MB)
        if len(out_start) < 200:
            out_start += block
            out_start = out_start[:200]

        if not block:
            break
        hash_out.update(block)
    assert in_start == out_start
    assert hash_out.hexdigest() == hash_in.hexdigest()

    message = mesh_client_two.retrieve_message(message_id)
    message.status = "acknowledged"  # type: ignore[attr-defined]


def test_send_receive_legacy_mapping(
    local_mesh_bucket: Bucket,
    sfn: SFNClient,
    events: EventBridgeClient,
    ssm: SSMClient,
    lambdas: LambdaClient,
    mesh_client_two: MeshClient,
):
    wait_till_not_running(state_machine_arn=GET_MESSAGES_SFN_ARN, sfn=sfn)

    sender = LOCAL_MAILBOXES[0]
    recipient = LOCAL_MAILBOXES[1]
    workflow_id = f"{uuid4().hex[:8]} {uuid4().hex[:8]}"
    filename = f"{uuid4().hex}.dat"
    key = f"outbound_{sender}_to_{recipient}/{filename}"
    s3_object = local_mesh_bucket.Object(key)
    content = b"test"
    content_type = "text/plain"

    with temp_mapping_for_s3_object(  # noqa: SIM117, RUF100
        s3_object, sender, recipient, workflow_id, ssm
    ):
        with CloudwatchLogsCapture(log_group=SEND_LOG_GROUP) as cw:
            s3_object.put(Body=content, ContentType=content_type)
            events.put_events(
                Entries=[sample_trigger_event(local_mesh_bucket.name, key)]
            )  # no cloudtrail in localstack
            cw.wait_for_logs(predicate=lambda x: x.get("logReference") == "LAMBDA0003")

    wait_for(lambda: len(mesh_client_two.list_messages()) > 0, timeout=30)
    messages = mesh_client_two.list_messages()
    assert len(messages) == 1
    message_id = messages[0]

    message = mesh_client_two.retrieve_message(message_id)
    assert message.message_id == message_id
    assert message.read() == content
    assert message.filename == f"{message_id}.dat"  # default if no mex-filename
    assert message.workflow_id == workflow_id

    with CloudwatchLogsCapture(log_group=POLL_LOG_GROUP) as cw:
        response = lambdas.invoke(
            FunctionName=POLL_FUNCTION,
            InvocationType="RequestResponse",
            LogType="Tail",
            Payload=json.dumps({"mailbox": recipient}).encode("utf-8"),
        )

        cw.wait_for_logs(
            predicate=lambda x: x.get("logReference") in ("LAMBDA0003", "LAMBDA9999")
        )

    response_payload, _ = sync_json_lambda_invocation_successful(response)
    assert response_payload
    assert response_payload.get("statusCode") == 200
    body = response_payload.get("body")
    assert body
    assert body["message_count"] == 1

    received = body["message_list"][0]["body"]
    assert received["message_id"] == message_id

    message = mesh_client_two.retrieve_message(message_id)
    message.status = "acknowledged"  # type: ignore[attr-defined]
