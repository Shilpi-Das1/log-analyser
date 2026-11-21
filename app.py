"""Flask web app: JSON API + single-page UI."""
import os
import re

from flask import Flask, jsonify, make_response, render_template, request

from core import config, llm, parsers, service
from core.errors import AppError

_SAFE_NAME = re.compile(r"[^\w.\- ]")


def _uploads_dir():
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return config.UPLOADS_DIR


def _safe_filename(name: str) -> str:
    """Sanitise an upload filename to path-safe characters."""
    name = _SAFE_NAME.sub("_", name or "log")
    return name[:120] or "log"


def _save_upload(text: str, name: str) -> dict:
    """Persist *text* to UPLOADS_DIR, pruning oldest files when over MAX_UPLOADS."""
    d = _uploads_dir()
    safe = _safe_filename(name)
    path = d / safe
    # Avoid collisions with a numeric suffix
    stem, _, ext = safe.rpartition(".")
    stem = stem or safe
    ext = ("." + ext) if ext and ext != stem else ""
    counter = 1
    while path.exists():
        path = d / f"{stem}_{counter}{ext}"
        counter += 1
    path.write_text(text, encoding="utf-8")
    # Prune oldest when over the cap
    files = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime)
    for old in files[:max(0, len(files) - config.MAX_UPLOADS)]:
        old.unlink(missing_ok=True)
    return {"name": path.name, "size": len(text)}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


@app.errorhandler(AppError)
def _app_error(exc: AppError):
    return jsonify({"error": str(exc)}), exc.status


@app.errorhandler(413)
def _too_large(_exc):
    return jsonify({"error": "Upload too large (limit 25 MB)."}), 413


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _log_from_request() -> tuple[str, str, str]:
    """(project, log_text, name) from a multipart upload or a JSON body."""
    upload = request.files.get("file")
    if upload:
        return (request.form.get("project", ""), upload.read().decode("utf-8", errors="replace"),
                upload.filename or "log")
    data = _body()
    return data.get("project", ""), data.get("log", ""), data.get("name", "pasted log")


@app.get("/")
def index():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Surrogate-Control"] = "no-store"
    resp.headers["Vary"] = "*"
    return resp


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.svg")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/api/status")
def status():
    return jsonify({
        "llm_configured": llm.configured(),
        "llm_backend": config.LLM_BACKEND,
        "github_token": bool(config.GITHUB_TOKEN),
        "github_dry_run": config.GITHUB_DRY_RUN,
        "confidence_threshold": config.CONFIDENCE_THRESHOLD,
        "log_formats": ["auto", *parsers.FORMATS],
    })


@app.get("/api/samples")
def samples():
    items = []
    if config.SAMPLES_DIR.is_dir():
        for path in sorted(p for p in config.SAMPLES_DIR.iterdir() if p.suffix in (".log", ".xml")):
            items.append({"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")})
    return jsonify(items)


# ── uploads (saved log files) ─────────────────────────────────────────

@app.get("/api/uploads")
def list_uploads():
    d = _uploads_dir()
    files = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([{"name": p.name, "size": p.stat().st_size,
                     "mtime": p.stat().st_mtime} for p in files])


@app.get("/api/uploads/<name>")
def get_upload(name):
    safe = _safe_filename(name)
    path = _uploads_dir() / safe
    if not path.exists() or not path.is_file():
        raise AppError("Upload not found.")
    return jsonify({"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")})


@app.delete("/api/uploads/<name>")
def delete_upload(name):
    safe = _safe_filename(name)
    path = _uploads_dir() / safe
    if path.exists() and path.is_file():
        path.unlink()
    return jsonify({"deleted": safe})


# ── projects ─────────────────────────────────────────────────────────

@app.get("/api/projects")
def list_projects():
    return jsonify(service.list_projects())


@app.get("/api/projects/available")
def available_projects():
    """Directories under the projects root that can be registered."""
    root = config.PROJECTS_ROOT
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) \
        if root.is_dir() else []
    return jsonify(names)


@app.post("/api/projects")
def add_project():
    d = _body()
    return jsonify(service.add_project(d.get("name", ""), d.get("path", ""), d.get("log_format", "auto"),
                                       d.get("github_repo", ""), d.get("domain_hint", ""))), 201


@app.post("/api/projects/clone")
def clone_project():
    d = _body()
    from core import projects as _proj
    result = _proj.clone_project(
        git_url=d.get("git_url", ""),
        name=d.get("name", ""),
        token=d.get("token", ""),
        log_format=d.get("log_format", "auto"),
        github_repo=d.get("github_repo", ""),
        domain_hint=d.get("domain_hint", ""),
    )
    return jsonify(result), 201


@app.post("/api/projects/<name>/refresh")
def refresh_project(name):
    return jsonify(service.refresh_project(name))


@app.delete("/api/projects/<name>")
def remove_project(name):
    service.remove_project(name)
    return jsonify({"removed": name})


# ── runs ─────────────────────────────────────────────────────────────

@app.post("/api/runs")
def submit_log():
    project, text, name = _log_from_request()
    is_file = bool(request.files.get("file"))
    fields = request.form if is_file else _body()
    source = fields.get("source") or "upload"
    meta = {k: fields.get(k) for k in service.META_KEYS}
    # Auto-save files uploaded by a person (pasted text and CI posts are not saved)
    if is_file and source == "upload":
        _save_upload(text, name)
    return jsonify(service.submit_log(project, text, name, source, meta))


@app.get("/api/runs")
def list_runs():
    return jsonify(service.list_runs(request.args.get("project", "")))


@app.get("/api/runs/<run_id>")
def get_run(run_id):
    return jsonify(service.get_run(run_id))


@app.post("/api/runs/<run_id>/analyse")
def analyse(run_id):
    return jsonify(service.analyse_failure(run_id, _body().get("test_id", "")))


# ── cases ────────────────────────────────────────────────────────────

@app.get("/api/case/<case_id>")
def case(case_id):
    return jsonify(service.get_case(case_id))


@app.post("/api/case/<case_id>/info")
def info(case_id):
    d = _body()
    return jsonify(service.provide_info(
        case_id, d.get("answer", ""), d.get("question", ""),
        bool(d.get("force_continue", False))))


@app.post("/api/case/<case_id>/patch")
def patch(case_id):
    return jsonify(service.generate_patch(case_id, _body().get("notes", "")))


@app.post("/api/case/<case_id>/rerun")
def rerun(case_id):
    upload = request.files.get("file")
    text = upload.read().decode("utf-8", errors="replace") if upload else _body().get("log", "")
    return jsonify(service.submit_rerun(case_id, text))


@app.post("/api/case/<case_id>/kb")
def save_kb(case_id):
    return jsonify(service.save_to_kb(case_id))


@app.post("/api/case/<case_id>/draft/<kind>")
def draft(case_id, kind):
    if kind == "issue":
        return jsonify(service.draft_issue(case_id, force=_body().get("force") is True))
    if kind == "pr":
        return jsonify(service.draft_pr(case_id))
    raise AppError("Unknown draft type.")


@app.put("/api/case/<case_id>/draft/<kind>")
def edit_draft(case_id, kind):
    d = _body()
    return jsonify(service.update_draft(case_id, kind, d.get("title", ""), d.get("body", "")))


@app.post("/api/case/<case_id>/publish/<kind>")
def publish(case_id, kind):
    approved = _body().get("approved") is True
    if kind == "issue":
        return jsonify(service.create_issue(case_id, approved))
    if kind == "pr":
        return jsonify(service.create_pr(case_id, approved))
    raise AppError("Unknown publish type.")


# ── knowledge base ───────────────────────────────────────────────────

@app.get("/api/kb")
def kb_list():
    return jsonify(service.kb_list(request.args.get("project", "")))


@app.delete("/api/kb/<int:entry_id>")
def kb_delete(entry_id):
    service.kb_delete(entry_id, request.args.get("project", ""))
    return jsonify({"deleted": entry_id})


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")))
