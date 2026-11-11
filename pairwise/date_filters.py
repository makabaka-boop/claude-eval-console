"""Shared calendar-date range validation for the Pair-wise lists."""

import re
from datetime import date


def date_range(h, query):
    values = []
    for key in ("date_start", "date_end"):
        value = str(query.get(key, [""])[0]).strip()
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise h.WorkflowError("日期范围无效，请使用 YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise h.WorkflowError("日期范围无效，请使用有效日期") from exc
        values.append(value)
    start, end = values
    if start and end and start > end:
        raise h.WorkflowError("开始日期不能晚于结束日期")
    return start, end


def includes(value, start, end):
    """Match the calendar date displayed from a stored timestamp."""
    day = str(value or "")[:10]
    return bool(day) and (not start or day >= start) and (not end or day <= end)
