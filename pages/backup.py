"""Settings > Backup: status, "Back up now", downloads, and a JSON status.

Routes (endpoint names are what templates and other pages use):
  GET  /settings-ui/backup          backup_page      owner
  POST /settings/backup/run         backup_run       owner, once per 5 minutes
  GET  /settings/backup/status      backup_status    owner (JSON, for the dashboard)
  GET  /settings/backup/<name>      backup_download  owner who is a platform admin,
                                                     or any owner when
                                                     SINGLE_BUSINESS_INSTALL is true

A dump holds the whole database. On a shared server that is every
business, so a business owner may download only on a one-shop install.
"""
from flask import (abort, current_app, flash, g, jsonify, redirect, render_template,
                   send_from_directory, session, url_for)

from auth.auth_middleware import superuser_api_required, superuser_required
from models import db
from services import audit_service, backup
from verticals.features import feature_required


def single_business():
    """A shop's own server: the flag is set AND the database really holds one
    business. The count matters: a dump holds every business, so a second
    business signed up on the same server must never be able to download it."""
    from models import Tenant
    return bool(current_app.config.get("SINGLE_BUSINESS_INSTALL")) and Tenant.query.count() <= 1


def can_download():
    return bool(g.get("is_platform_admin")) or single_business()


def _audit(action, name):
    audit_service.log(session.get("user_id"), action, "Backup", None, None, {"name": name})
    db.session.commit()


def register(app):
    @app.route("/settings-ui/backup")
    @superuser_required
    @feature_required("backup")
    def backup_page():
        from app import get_user_context
        dest = backup.default_dir()
        return render_template("backup.html", **get_user_context(),
                               status=backup.backup_status(dest),
                               backups=backup.list_backups(dest)[:30],
                               can_download=can_download(),
                               human_size=backup.human_size, age_text=backup.age_text,
                               offsite=bool(app.config.get("BACKUP_RCLONE_REMOTE")))

    @app.route("/settings/backup/run", methods=["POST"])
    @superuser_required
    @feature_required("backup")
    def backup_run():
        dest = backup.default_dir()
        wait = backup.too_soon(dest)
        if wait:
            flash(f"A backup was made less than 5 minutes ago. Try again in "
                  f"{wait // 60 + 1} min.", "warning")
            return redirect(url_for("backup_page"), 303)
        try:
            manifest = backup.create_backup(dest)
        except backup.BackupError as e:
            app.logger.error(f"backup now failed: {e}")
            flash(f"Backup failed: {e}", "danger")
            return redirect(url_for("backup_page"), 303)
        _audit("backup.create", manifest["name"])
        message = f"Backup {manifest['name']} made ({backup.human_size(manifest['size'])})."
        offsite = manifest.get("offsite")
        if offsite is not None and not offsite["ok"]:
            flash(message + f" The offsite copy failed: {offsite['message']}", "warning")
        else:
            flash(message, "success")
        return redirect(url_for("backup_page"), 303)

    @app.route("/settings/backup/status")
    @superuser_api_required
    @feature_required("backup")
    def backup_status():
        return jsonify(backup.backup_status(backup.default_dir()))

    @app.route("/settings/backup/<name>")
    @superuser_required
    @feature_required("backup")
    def backup_download(name):
        if not backup.is_backup_name(name):
            abort(404)
        if not can_download():
            return jsonify({"error": "Forbidden",
                            "message": "Only a platform admin may download backups on a "
                                       "shared server."}), 403
        dest = backup.default_dir()
        if not (dest / name).is_file():
            abort(404)
        _audit("backup.download", name)
        return send_from_directory(dest, name, as_attachment=True,
                                   mimetype="application/gzip")
