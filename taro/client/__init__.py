import json
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Tuple, Any, Dict, NamedTuple, Optional, TypeVar, Generic

from taro.jobs.api import API_FILE_EXTENSION
from taro.jobs.inst import JobInst, JobInstanceMetadata
from taro.socket import SocketClient, ServerResponse, Error

log = logging.getLogger(__name__)


class APIInstanceResponse(NamedTuple):
    instance_meta: JobInstanceMetadata
    body: Dict[str, Any]


class APIErrorType(Enum):
    SOCKET = auto()
    RESPONSE = auto()
    MISSING_RESPONSE_METADATA = auto()


@dataclass
class APIError:
    api_id: str
    error: APIErrorType
    socket_error: Optional[Error]
    resp_error: Dict[str, Any]


T = TypeVar('T')


@dataclass
class MultiResponse(Generic[T]):

    responses: List[T]
    errors: List[APIError]

    def __iter__(self):
        return iter((self.responses, self.errors))


@dataclass
class JobInstanceResponse:
    instance_metadata: JobInstanceMetadata


@dataclass
class ReleaseResponse(JobInstanceResponse):
    pass


@dataclass
class StopResponse(JobInstanceResponse):
    result_str: str


@dataclass
class TailResponse(JobInstanceResponse):
    tail: List[str]


def read_job_instances(instance_match=None) -> MultiResponse[JobInst]:
    with JobsClient() as client:
        return client.read_job_instances(instance_match)


def release_waiting_jobs(instance_match, waiting_state) -> MultiResponse[ReleaseResponse]:
    with JobsClient() as client:
        return client.release_waiting_jobs(instance_match, waiting_state)

def release_pending_jobs(pending_group, instance_match=None) -> MultiResponse[ReleaseResponse]:
    with JobsClient() as client:
        return client.release_pending_jobs(pending_group, instance_match)


def stop_jobs(instance_match) -> MultiResponse[StopResponse]:
    with JobsClient() as client:
        return client.stop_jobs(instance_match)


def read_tail(instance_match=None) -> MultiResponse[TailResponse]:
    with JobsClient() as client:
        return client.read_tail(instance_match)


class JobsClient(SocketClient):

    def __init__(self):
        super().__init__(API_FILE_EXTENSION, bidirectional=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _send_request(self, api: str, instance_match=None, req_body=None) \
            -> Tuple[List[APIInstanceResponse], List[APIError]]:
        if not req_body:
            req_body = {}
        req_body["request_metadata"] = {"api": api}
        if instance_match and instance_match.id_criteria:
            req_body["request_metadata"]["instance_match"] = instance_match.to_dict()

        server_responses: List[ServerResponse] = self.communicate(json.dumps(req_body))
        return _process_responses(server_responses)

    def read_job_instances(self, instance_match=None) -> MultiResponse[JobInst]:
        instance_responses, api_errors = self._send_request('/jobs', instance_match)
        return MultiResponse([JobInst.from_dict(body["job_info"]) for _, body in instance_responses], api_errors)

    def release_waiting_jobs(self, instance_match, waiting_state) -> MultiResponse[ReleaseResponse]:
        if not instance_match or not waiting_state:
            raise ValueError("Arguments cannot be empty")

        req_body = {"waiting_state": waiting_state.name}
        instance_responses, api_errors =\
            self._send_request('/jobs/release/waiting', instance_match, req_body)
        return MultiResponse([ReleaseResponse(meta) for meta, body in instance_responses if body["released"]],
                             api_errors)

    def release_pending_jobs(self, pending_group, instance_match=None) -> MultiResponse[ReleaseResponse]:
        if not pending_group:
            raise ValueError("Missing pending group")

        req_body = {"pending_group": pending_group}
        instance_responses, api_errors =\
            self._send_request('/jobs/release/pending', instance_match, req_body)
        return MultiResponse([ReleaseResponse(meta) for meta, body in instance_responses if body["released"]],
                             api_errors)

    def stop_jobs(self, instance_match) -> MultiResponse[StopResponse]:
        """

        :param instance_match: instance id matching criteria is mandatory for the stop operation
        :return: list of tuple[instance-id, stop-result]
        """
        if not instance_match:
            raise ValueError('Id matching criteria is mandatory for the stop operation')

        instance_responses, api_errors = self._send_request('/jobs/stop', instance_match)
        return MultiResponse([StopResponse(meta, body["result"]) for meta, body in instance_responses], api_errors)

    def read_tail(self, instance_match) -> MultiResponse[TailResponse]:
        instance_responses, api_errors = self._send_request('/jobs/tail', instance_match)
        return MultiResponse([TailResponse(meta, body["tail"]) for meta, body in instance_responses], api_errors)


def _process_responses(responses) -> Tuple[List[APIInstanceResponse], List[APIError]]:
    instance_responses: List[APIInstanceResponse] = []
    api_errors: List[APIError] = []

    for server_id, resp, error in responses:
        if error:
            log.error("event=[response_error] type=[socket] error=[%s]", error)
            api_errors.append(APIError(server_id, APIErrorType.SOCKET, error, {}))
            continue

        resp_body = json.loads(resp)
        resp_metadata = resp_body.get("response_metadata")
        if not resp_metadata:
            log.error("event=[response_error] error=[missing response metadata]")
            api_errors.append(APIError(server_id, APIErrorType.MISSING_RESPONSE_METADATA, None, {}))
            continue
        if "error" in resp_metadata:
            log.error("event=[response_error] type=[api] error=[%s]", resp_metadata["error"])
            api_errors.append(APIError(server_id, APIErrorType.RESPONSE, None, resp_metadata["error"]))
            continue

        for instance_resp in resp_body['instances']:
            instance_metadata = JobInstanceMetadata.from_dict(instance_resp['instance_metadata'])
            instance_responses.append(APIInstanceResponse(instance_metadata, instance_resp))

    return instance_responses, api_errors
