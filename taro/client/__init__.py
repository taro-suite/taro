import json
import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Tuple, Any, Dict, NamedTuple, Optional, TypeVar, Generic

from taro import dto
from taro.jobs.api import API_FILE_EXTENSION
from taro.jobs.job import JobInfo, JobInstanceID
from taro.socket import SocketClient, ServerResponse, Error

log = logging.getLogger(__name__)


class APIInstanceResponse(NamedTuple):
    id: JobInstanceID
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
    resp_error: dict[str, Any]


T = TypeVar('T')


@dataclass
class MultiResponse(Generic[T]):

    responses: List[T]
    errors: List[APIError]

    def __iter__(self):
        return iter((self.responses, self.errors))


@dataclass
class JobInstanceResponse:
    id: JobInstanceID


@dataclass
class ReleaseResponse(JobInstanceResponse):
    pass


@dataclass
class StopResponse(JobInstanceResponse):
    result_str: str


@dataclass
class TailResponse(JobInstanceResponse):
    tail: List[str]


def read_jobs_info(job_instance="") -> MultiResponse[JobInfo]:
    with JobsClient() as client:
        return client.read_jobs_info(job_instance)


def release_jobs(pending_group) -> MultiResponse[ReleaseResponse]:
    with JobsClient() as client:
        return client.release_jobs(pending_group)


def stop_jobs(instances, interrupt: bool) -> MultiResponse[StopResponse]:
    with JobsClient() as client:
        return client.stop_jobs(instances, interrupt)  # TODO ??


def read_tail(instance) -> MultiResponse[TailResponse]:
    with JobsClient() as client:
        return client.read_tail(instance)


class JobsClient(SocketClient):

    def __init__(self):
        super().__init__(API_FILE_EXTENSION, bidirectional=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _send_request(self, api: str, req_body=None, *, job_instance: str = '') \
            -> Tuple[List[APIInstanceResponse], List[APIError]]:
        if not req_body:
            req_body = {}
        req_body["request_metadata"] = {"api": api}
        if job_instance:
            req_body["request_metadata"]["match"] = {"ids": [job_instance]}

        server_responses: List[ServerResponse] = self.communicate(json.dumps(req_body))
        return _process_responses(server_responses)

    def read_jobs_info(self, job_instance="") -> MultiResponse[JobInfo]:
        instance_responses, api_errors = self._send_request('/jobs', job_instance=job_instance)
        return MultiResponse([dto.to_job_info(body["job_info"]) for _, body in instance_responses], api_errors)

    def release_jobs(self, pending_group) -> MultiResponse[ReleaseResponse]:
        instance_responses, api_errors = self._send_request('/jobs/release', {"pending_group": pending_group})
        return MultiResponse([ReleaseResponse(jid) for jid, body in instance_responses if body["released"]],
                             api_errors)

    def stop_jobs(self, instance) -> MultiResponse[StopResponse]:
        """

        :param instance:
        :return: list of tuple[instance-id, stop-result]
        """
        if not instance:
            raise ValueError('Instances to be stopped cannot be empty')

        instance_responses, api_errors = self._send_request('/jobs/stop', job_instance=instance)
        return MultiResponse([StopResponse(jid, body["result"]) for jid, body in instance_responses], api_errors)

    def read_tail(self, job_instance) -> MultiResponse[TailResponse]:
        instance_responses, api_errors = self._send_request('/jobs/tail', job_instance=job_instance)
        return MultiResponse([TailResponse(jid, body["tail"]) for jid, body in instance_responses], api_errors)


def _process_responses(responses) -> Tuple[List[APIInstanceResponse], List[APIError]]:
    instance_responses: List[APIInstanceResponse] = []
    api_errors: List[APIError] = []

    for server_id, resp, error in responses:
        if error:
            log.error("event=[response_error] type=[socket] error=[%s]", error)
            api_errors.append(APIError(server_id, APIErrorType.SOCKET, error, {}))
            continue

        resp_body = json.loads(resp)
        inst_metadata = resp_body.get("response_metadata")
        if not inst_metadata:
            log.error("event=[response_error] error=[missing response metadata]")
            api_errors.append(APIError(server_id, APIErrorType.MISSING_RESPONSE_METADATA, None, {}))
            continue
        if "error" in inst_metadata:
            log.error("event=[response_error] type=[api] error=[%s]", inst_metadata["error"])
            api_errors.append(APIError(server_id, APIErrorType.RESPONSE, None, resp_body["error"]))
            continue

        for instance_resp in resp_body['instances']:
            inst_metadata = instance_resp['instance_metadata']
            jid = JobInstanceID(inst_metadata["job_id"], inst_metadata["instance_id"])
            instance_responses.append(APIInstanceResponse(jid, instance_resp))

    return instance_responses, api_errors
