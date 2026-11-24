"""Parsing and validation for catalogue submissions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LINE_NO_KEYS = ("line_no", "lineNo", "lineno", "row_no", "number", "行号", "序号")
OLD_NAME_KEYS = ("old_name", "oldName", "old", "旧名", "旧地名")
NEW_NAME_KEYS = ("new_name", "newName", "new", "今名", "新名", "新地名", "现名")
EVIDENCE_KEYS = (
    "evidence",
    "evidence_groups",
    "groups",
    "archive_groups",
    "archives",
    "archive_nos",
    "依据档号",
    "档号组",
    "依据",
)
ROW_KEYS = ("rows", "entries", "lines", "records", "data", "行")
PUBLISH_NO_KEYS = ("publish_no", "publishNo", "release_no", "发布号", "发布编号")
SOURCE_NAME_KEYS = ("source_name", "sourceName", "来源", "来源名称", "资料来源")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(ValueError):
    """A submission cannot be accepted; nothing may be stored."""


@dataclass
class Submission:
    publish_no: str
    source_name: str | None
    rows: list[dict] = field(default_factory=list)

    @property
    def line_count(self) -> int:
        return len(self.rows)


def _first(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _scalar_text(value, what: str, *, allow_empty: bool = False) -> str:
    if isinstance(value, bool):
        raise ValidationError(f"{what} 不能是布尔值")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise ValidationError(f"{what} 必须是字符串或数字")
    value = value.strip()
    if not value and not allow_empty:
        raise ValidationError(f"{what} 不能为空")
    return value


def _publish_no(value) -> str:
    text = _scalar_text(value, "发布号")
    if len(text) > 200:
        raise ValidationError("发布号不能超过 200 个字符")
    if _CONTROL.search(text) or "/" in text or "\\" in text:
        raise ValidationError("发布号不能包含斜杠或控制字符")
    return text


def _parse_group(group, where: str) -> list[str]:
    if isinstance(group, dict):
        # Tolerate {"archives": [...]} / {"档号": [...]} group objects.
        inner = _first(group, EVIDENCE_KEYS + ("档号", "archive_no", "号码"))
        if inner is None:
            raise ValidationError(f"{where} 必须是档号列表")
        group = inner
    if not isinstance(group, (list, tuple)):
        group = [group]
    numbers: list[str] = []
    seen: set[str] = set()
    for item in group:
        if isinstance(item, (list, tuple, dict)):
            raise ValidationError(f"{where} 中不能嵌套列表或对象")
        number = _scalar_text(item, f"{where} 的档号")
        if number not in seen:  # a repeated number inside one group adds nothing
            seen.add(number)
            numbers.append(number)
    if not numbers:
        raise ValidationError(f"{where} 不能是空组")
    return numbers


def _parse_evidence(value, where: str) -> list[list[str]]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return [_parse_group(value, where)]
    if not value:
        raise ValidationError(f"{where} 至少需要一组依据档号")

    if all(isinstance(item, (list, tuple, dict)) for item in value):
        groups = [
            _parse_group(item, f"{where} 第 {idx} 组")
            for idx, item in enumerate(value, start=1)
        ]
    elif any(isinstance(item, (list, tuple, dict)) for item in value):
        raise ValidationError(f"{where} 不能混用档号与档号组")
    else:
        groups = [_parse_group(value, where)]

    if not groups:
        raise ValidationError(f"{where} 至少需要一组依据档号")
    return groups


def parse_submission(
    payload,
    *,
    form_publish_no: str | None = None,
    form_source_name: str | None = None,
) -> Submission:
    """Normalise a JSON submission, accepting both English and Chinese field names."""

    if payload is None:
        raise ValidationError("没有收到 JSON 数据")
    if isinstance(payload, list):
        payload = {"rows": payload}
    if not isinstance(payload, dict):
        raise ValidationError("JSON 顶层必须是对象或行数组")

    json_publish_no = _first(payload, PUBLISH_NO_KEYS)
    if form_publish_no is not None and str(form_publish_no).strip():
        candidate_form = _publish_no(form_publish_no)
        if json_publish_no is not None:
            candidate_json = _publish_no(json_publish_no)
            if candidate_form != candidate_json:
                raise ValidationError("表单中的发布号与 JSON 中的发布号不一致")
        publish_no = candidate_form
    elif json_publish_no is not None:
        publish_no = _publish_no(json_publish_no)
    else:
        raise ValidationError("缺少发布号（publish_no）")

    raw_rows = _first(payload, ROW_KEYS)
    if raw_rows is None:
        raise ValidationError("缺少行数据（rows）")
    if not isinstance(raw_rows, (list, tuple)):
        raise ValidationError("rows 必须是数组")
    if not raw_rows:
        raise ValidationError("rows 不能为空")

    source_raw = _first(payload, SOURCE_NAME_KEYS)
    if form_source_name is not None and str(form_source_name).strip():
        source_name = str(form_source_name).strip()
    elif source_raw is not None:
        source_name = _scalar_text(source_raw, "来源名称", allow_empty=True) or None
    else:
        source_name = None

    rows: list[dict] = []
    seen_line_nos: set[str] = set()
    for idx, raw in enumerate(raw_rows, start=1):
        where = f"第 {idx} 行"
        if not isinstance(raw, dict):
            raise ValidationError(f"{where} 必须是对象")

        line_raw = _first(raw, LINE_NO_KEYS)
        if line_raw is None:
            raise ValidationError(f"{where} 缺少行号")
        line_no = _scalar_text(line_raw, f"{where} 的行号")
        if line_no in seen_line_nos:
            raise ValidationError(f"行号重复：{line_no}")
        seen_line_nos.add(line_no)

        old_raw = _first(raw, OLD_NAME_KEYS)
        if old_raw is None:
            raise ValidationError(f"{where}（行号 {line_no}）缺少旧名")
        new_raw = _first(raw, NEW_NAME_KEYS)
        if new_raw is None:
            raise ValidationError(f"{where}（行号 {line_no}）缺少今名")
        old_name = _scalar_text(old_raw, f"{where} 的旧名", allow_empty=True)
        new_name = _scalar_text(new_raw, f"{where} 的今名", allow_empty=True)

        evidence_raw = _first(raw, EVIDENCE_KEYS)
        if evidence_raw is None:
            raise ValidationError(f"{where}（行号 {line_no}）缺少依据档号")
        groups = _parse_evidence(
            evidence_raw, f"{where}（行号 {line_no}）的依据档号"
        )

        rows.append(
            {
                "line_no": line_no,
                "old_name": old_name,
                "new_name": new_name,
                "evidence": groups,
            }
        )

    return Submission(publish_no=publish_no, source_name=source_name, rows=rows)
