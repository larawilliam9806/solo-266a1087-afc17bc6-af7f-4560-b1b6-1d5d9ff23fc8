"""Gazetteer old-place-name archive supply service."""

from __future__ import annotations

import os

from flask import Flask

from . import db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    configured_db = app.config.get("ARCHIVE_DB")
    if configured_db:
        db.configure(configured_db)
    db.init_db(configured_db or db.db_path())

    from .routes import bp

    app.register_blueprint(bp)
    return app
