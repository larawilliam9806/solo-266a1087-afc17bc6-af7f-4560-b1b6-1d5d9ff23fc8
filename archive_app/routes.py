"""HTTP routes for cataloguing, withdrawal, and reader download."""

from __future__ import annotations

import json
from urllib.parse import quote

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

from . import db, export
from .submission import ValidationError, parse_submission

bp = Blueprint("main", __name__)

MAX_JSON_BYTES = 32 * 1024 * 1024


def _load_payload():
    """Read the submission JSON from a file field, a textarea, or a raw body."""
    upload = request.files.get("json_file")
    if upload is not None and upload.filename:
        raw = upload.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValidationError("JSON 文件过大")
        if not raw.strip():
            raise ValidationError("上传的 JSON 文件为空")
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except UnicodeDecodeError:
            raise ValidationError("JSON 文件必须是 UTF-8 编码")
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列")

    text = (request.form.get("payload") or "").strip()
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列")

    data = request.get_json(silent=True)
    if data is not None:
        return data

    raise ValidationError("请上传 JSON 文件或粘贴 JSON 内容")


def _is_api_request() -> bool:
    """JSON clients get JSON; anything else (browser, curl form) gets a page."""
    return request.is_json


def _page(status: int, message: str, category: str = "error"):
    return (
        render_template(
            "index.html",
            publications=db.list_publications(),
            withdrawals=db.list_withdrawals(),
            message=message,
            message_category=category,
        ),
        status,
    )


def _fail(message: str, status: int):
    if _is_api_request():
        return {"ok": False, "error": message}, status
    return _page(status, message, "error")


@bp.get("/")
def index():
    return render_template(
        "index.html",
        publications=db.list_publications(),
        withdrawals=db.list_withdrawals(),
        message=None,
        message_category=None,
    )


@bp.post("/publish")
@bp.post("/catalog/publish")
def catalog_publish():
    try:
        payload = _load_payload()
        submission = parse_submission(
            payload,
            form_publish_no=request.form.get("publish_no"),
            form_source_name=request.form.get("source_name"),
        )
    except ValidationError as exc:
        return _fail(f"未保存任何数据：{exc}", 400)

    try:
        db.publish(
            submission.rows,
            submission.publish_no,
            submission.source_name,
        )
    except db.DuplicatePublication as exc:
        return _fail(str(exc), 409)
    except db.IncompleteEvidence as exc:
        shown = "、".join(exc.lines[:20])
        more = " 等" if len(exc.lines) > 20 else ""
        return _fail(
            f"整批退回：行 {shown}{more} 缺少完整依据档号组"
            f"（组内档号有已撤档的）。本批未保存任何数据。",
            409,
        )
    except Exception:
        current_app.logger.exception("保存发布号 %s 失败", submission.publish_no)
        return _fail("保存失败，本批未写入任何数据，请稍后重试。", 500)

    message = f"发布号 {submission.publish_no} 已保存，共 {submission.line_count} 行。"
    if _is_api_request():
        return {
            "ok": True,
            "publish_no": submission.publish_no,
            "rows": submission.line_count,
        }, 201
    flash(message, "success")
    return redirect(url_for("main.index"))


@bp.post("/withdraw")
@bp.post("/catalog/withdraw")
def catalog_withdraw():
    archive_no = (request.form.get("archive_no") or "").strip()
    if not archive_no and request.is_json:
        data = request.get_json(silent=True) or {}
        archive_no = str(data.get("archive_no") or data.get("档号") or "").strip()
    if not archive_no:
        return _fail("请填写要撤档的档号。", 400)
    if len(archive_no) > 500:
        return _fail("档号过长。", 400)

    try:
        created = db.withdraw(archive_no)
    except Exception:
        current_app.logger.exception("撤档 %s 失败", archive_no)
        return _fail(
            "撤档保存失败，仍沿用原撤档状态（本次撤档未生效）。", 500
        )

    if created:
        message = f"档号 {archive_no} 已撤档，后续下载即时生效。"
    else:
        message = f"档号 {archive_no} 此前已撤档，本次未重复登记。"

    if _is_api_request():
        return {
            "ok": True,
            "archive_no": archive_no,
            "newly_withdrawn": created,
            "message": message,
        }, 200
    flash(message, "success" if created else "info")
    return redirect(url_for("main.index"))


@bp.get("/download")
def download_lookup():
    publish_no = (request.args.get("publish_no") or "").strip()
    if not publish_no:
        return _page(400, "请填写发布号。", "error")
    return redirect(url_for("main.download", publish_no=publish_no))


@bp.get("/csv/<publish_no>")
@bp.get("/download/<publish_no>.csv")
def download(publish_no: str):
    opened = export.open_export(publish_no)
    if opened is None:
        abort(404)

    ascii_name = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "-_.") else "_"
        for ch in publish_no
    ) or "gazetteer"
    disposition = (
        f"attachment; filename=\"gazetteer-{ascii_name}.csv\"; "
        f"filename*=UTF-8''{quote(publish_no + '.csv', safe='')}"
    )
    response = Response(
        stream_with_context(export.iter_csv(opened)),
        mimetype="text/csv",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response


@bp.get("/healthz")
def healthz():
    with db.get_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True}


@bp.app_errorhandler(413)
def too_large(_exc):
    return _fail("上传内容过大。", 413)


@bp.app_errorhandler(404)
def not_found(_exc):
    if _is_api_request():
        return {"ok": False, "error": "未找到。"}, 404
    return render_template("404.html"), 404
