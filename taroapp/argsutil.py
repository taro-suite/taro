from datetime import timedelta, timezone, datetime, time
from enum import Enum
from typing import List, Callable, Set

from taro import JobInstanceID
from taro.jobs.execution import Flag, ExecutionStateFlag
from taro.jobs.job import IDMatchingCriteria, InstanceMatchingCriteria, compound_id_filter, IntervalCriteria, \
    LifecycleEvent, StateCriteria
from taro.util import DateTimeFormat


def id_matching_criteria(args, def_id_match_strategy) -> List[IDMatchingCriteria]:
    """
    :param args: cli args
    :param def_id_match_strategy: id match strategy used when not overridden by args TODO
    :return: list of ID match criteria or empty when args has no criteria
    """
    if args.instances:
        return [IDMatchingCriteria.parse_pattern(i, def_id_match_strategy) for i in args.instances]
    else:
        return []


def id_match(args, def_id_match_strategy) -> Callable[[JobInstanceID], bool]:
    return compound_id_filter(id_matching_criteria(args, def_id_match_strategy))


def interval_criteria_converted_utc(args, interval_event=LifecycleEvent.CREATED):
    criteria = []

    from_dt = None
    to_dt = None
    include_to = True

    if getattr(args, 'from', None):
        from_ = getattr(args, 'from')
        if isinstance(from_, datetime):
            from_dt = from_.astimezone(timezone.utc)
        else:  # Assuming it is datetime.date
            from_dt = datetime.combine(from_, time.min).astimezone(timezone.utc)

    if getattr(args, 'to', None):
        if isinstance(args.to, datetime):
            to_dt = args.to.astimezone(timezone.utc)
        else:  # Assuming it is datetime.date
            to_dt = datetime.combine(args.to + timedelta(days=1), time.min).astimezone(timezone.utc)
            include_to = False

    if from_dt or to_dt:
        criteria.append(IntervalCriteria(interval_event, from_dt, to_dt, include_to=include_to))

    if getattr(args, 'today', None):
        criteria.append(IntervalCriteria.today(interval_event, local_tz=True))

    if getattr(args, 'yesterday', None):
        criteria.append(IntervalCriteria.yesterday(interval_event, local_tz=True))

    if getattr(args, 'week', None):
        criteria.append(IntervalCriteria.week_back(interval_event, local_tz=True))

    if getattr(args, 'fortnight', None):
        criteria.append(IntervalCriteria.days_interval(interval_event, -14, local_tz=True))

    if getattr(args, 'month', None):
        criteria.append(IntervalCriteria.days_interval(interval_event, -31, local_tz=True))

    return criteria


def instance_state_criteria(args):
    flag_groups: List[Set[ExecutionStateFlag]] = []
    if getattr(args, 'success', False):
        flag_groups.append({Flag.SUCCESS})
    if getattr(args, 'nonsuccess', False):
        flag_groups.append({Flag.NONSUCCESS})
    if getattr(args, 'aborted', False):
        flag_groups.append({Flag.ABORTED})
    if getattr(args, 'incomplete', False):
        flag_groups.append({Flag.INCOMPLETE})
    if getattr(args, 'discarded', False):
        flag_groups.append({Flag.DISCARDED})
    if getattr(args, 'failed', False):
        flag_groups.append({Flag.FAILURE})
    warning = getattr(args, 'warning', None)

    return StateCriteria(flag_groups=flag_groups, warning=warning)


def instance_matching_criteria(args, def_id_match_strategy, interval_event=LifecycleEvent.CREATED) -> \
        InstanceMatchingCriteria:
    return InstanceMatchingCriteria(
        id_matching_criteria(args, def_id_match_strategy),
        interval_criteria_converted_utc(args, interval_event),
        instance_state_criteria(args))


class TimestampFormat(Enum):
    DATE_TIME = DateTimeFormat.DATE_TIME_MS_LOCAL_ZONE
    TIME = DateTimeFormat.TIME_MS_LOCAL_ZONE
    NONE = DateTimeFormat.NONE
    UNKNOWN = None

    def __repr__(self) -> str:
        return self.name.lower().replace("_", "-")

    @staticmethod
    def from_str(string: str) -> "TimestampFormat":
        if not string:
            return TimestampFormat.NONE

        string = string.upper().replace("-", "_")
        try:
            return TimestampFormat[string]
        except KeyError:
            return TimestampFormat.UNKNOWN
