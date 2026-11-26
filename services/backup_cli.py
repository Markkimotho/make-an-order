"""``flask backup ...`` commands (registered in app._register_cli)."""
import json

import click
from flask.cli import with_appcontext

from services import backup


def _dir(path):
    return path or backup.default_dir()


@click.group("backup", help="Database backups: create, list, verify, restore, rotate.")
def backup_cli():
    pass


@backup_cli.command("create")
@click.option("--dir", "dest", default=None, help="Folder (default BACKUP_DIR).")
@click.option("--if-older-than", "hours", type=float, default=None,
              help="Skip unless the newest backup is older than this many hours.")
@with_appcontext
def create_command(dest, hours):
    """Back up the database now (also copies offsite when BACKUP_RCLONE_REMOTE is set)."""
    dest = _dir(dest)
    if hours is not None:
        status = backup.backup_status(dest)
        if status["has_backup"] and status["age_hours"] < hours:
            click.echo(f"Skipped: newest backup {status['name']} is "
                       f"{status['age_hours']} h old (< {hours} h).")
            return
    try:
        manifest = backup.create_backup(dest)
    except backup.BackupError as e:
        raise click.ClickException(str(e))
    click.echo(f"Created {manifest['path']} ({backup.human_size(manifest['size'])}, "
               f"{manifest['total_rows']} rows, sha256 {manifest['sha256'][:12]}...)")
    offsite = manifest.get("offsite")
    if offsite is not None:
        state = "done" if offsite["ok"] else f"FAILED: {offsite['message']}"
        click.echo(f"Offsite copy to {offsite['remote']}: {state}")


@backup_cli.command("list")
@click.option("--dir", "dest", default=None, help="Folder (default BACKUP_DIR).")
@click.option("--json", "as_json", is_flag=True, help="Print the status as JSON.")
@with_appcontext
def list_command(dest, as_json):
    """List backups, newest first."""
    dest = _dir(dest)
    backups = backup.list_backups(dest)
    if as_json:
        click.echo(json.dumps(backup.backup_status(dest), indent=2))
        return
    if not backups:
        click.echo(f"No backups in {dest}")
        return
    for b in backups:
        rows = (b["manifest"] or {}).get("total_rows", "?")
        click.echo(f"{b['name']}  {backup.human_size(b['size']):>9}  {rows:>8} rows  "
                   f"{backup.age_text(b['created_at'])}")
    status = backup.backup_status(dest)
    if status["stale"]:
        click.echo(f"WARNING: {status['message']}")


@backup_cli.command("verify")
@click.argument("file")
@with_appcontext
def verify_command(file):
    """Restore FILE (or 'latest') into a scratch database and compare row counts."""
    if file == "latest":
        newest = backup.latest_backup(backup.default_dir())
        if newest is None:
            raise click.ClickException("No backups to verify.")
        file = str(newest["path"])
    try:
        result = backup.verify_backup(file, dest_dir=backup.default_dir())
    except backup.BackupError as e:
        raise click.ClickException(str(e))
    for message in result["messages"]:
        click.echo(message)
    if not result["ok"]:
        raise click.ClickException(f"{result['name']} FAILED verification")
    click.echo(f"{result['name']}: {'checked (restore test skipped)' if result['skipped'] else 'OK'}")


@backup_cli.command("restore")
@click.argument("file")
@click.option("--yes", is_flag=True, help="Do not ask 'are you sure'.")
@click.option("--confirm-name", default=None,
              help="The database name, instead of typing it at the prompt.")
@with_appcontext
def restore_command(file, yes, confirm_name):
    """Replace the whole database with FILE. Stop the app first."""
    try:
        name = backup.database_name()
    except backup.BackupError as e:
        raise click.ClickException(str(e))
    if not yes:
        click.confirm(f"This replaces every business's data in {name} with {file}. "
                      "The app must be stopped. Continue?", abort=True)
    typed = confirm_name if confirm_name is not None else click.prompt(
        f"Type the database name ({name}) to confirm")
    try:
        result = backup.restore_backup(file, confirm=typed.strip(), dest_dir=backup.default_dir())
    except backup.BackupError as e:
        raise click.ClickException(str(e))
    click.echo(f"Restored {result['database']} from {result['restored']}. "
               f"The database as it was before is in {result['pre_restore']}.")
    click.echo("Next: flask db upgrade, then start the app.")


@backup_cli.command("rotate")
@click.option("--dir", "dest", default=None, help="Folder (default BACKUP_DIR).")
@click.option("--keep-daily", default=14, show_default=True)
@click.option("--keep-weekly", default=8, show_default=True)
@with_appcontext
def rotate_command(dest, keep_daily, keep_weekly):
    """Keep the newest backup per day (14 days) and per week (8 weeks)."""
    deleted = backup.rotate(_dir(dest), keep_daily=keep_daily, keep_weekly=keep_weekly)
    click.echo(f"Deleted {len(deleted)} old backups" + (": " + ", ".join(deleted) if deleted else ""))
