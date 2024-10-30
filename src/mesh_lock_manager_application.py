from shared.application import MESHLambdaApplication


class MeshLockManagerApplication(MESHLambdaApplication):
    """
    Lambda for interacting with locks in the dynamo db table
    """

    def initialise(self):
        self.response = self.event.raw_event
        self.lock_name = self.event.get("lock_name") or None
        self.execution_id = self.event.get("execution_id") or None

    def start(self):
        print(self.operation)
        if self.execution_id and self.lock_name:
            if self.operation == "remove":
                self._release_lock(
                    self.lock_name,
                    self.execution_id,
                )
        else:
            self.log_object.write_log(
                "MESHSEND0015",
                None,
                {"lock_name": self.lock_name, "owner_id": self.execution_id},
            )
        return

    def process_event(self, event):
        print("EVENT", event)
        event_detail = event.get("EventDetail", {})
        operation = event.get("Operation")
        self.operation = self.EVENT_TYPE(operation).raw_event
        return self.EVENT_TYPE(event_detail)


app = MeshLockManagerApplication()


def lambda_handler(event, context):
    """Standard lambda_handler"""
    return app.main(event, context)
