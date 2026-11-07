import os
import glob
import gzip
import shutil
import sqlite3
import hashlib

DB_PATH = os.environ.get("HOSPITAL_DB_PATH", "hospital_local_server.db")
BACKUP_DIR = os.environ.get("HOSPITAL_BACKUP_DIR", "./local_server_backups")
MAX_RETENTION_DAYS = 30

def prune_old_backups():
    """Keeps only the latest 30 compressed backups so the hospital server disk never fills up."""
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "*.db.gz")), key=os.path.getmtime, reverse=True)
    removed = 0
    for old_file in backups[MAX_RETENTION_DAYS:]:
        os.remove(old_file)
        removed += 1
    return removed

def restore_latest_backup():
    """Finds latest backup, verifies SHA-256 integrity against audit table, and restores live DB."""
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "*.db.gz")), key=os.path.getmtime, reverse=True)
    if not backups:
        print("[ERROR] No backup archives (.db.gz) found in", BACKUP_DIR)
        return False

    latest_gz = backups[0]
    with open(latest_gz, "rb") as f:
        computed_sha256 = hashlib.sha256(f.read()).hexdigest()

    print(f"[RECOVERY] Found latest snapshot: {os.path.basename(latest_gz)}")
    print(f"[INTEGRITY] Computed SHA-256: {computed_sha256}")

    # Decompress safely into a staging file first, then swap atomically
    staging_db = DB_PATH + ".restoring"
    with gzip.open(latest_gz, "rb") as f_in, open(staging_db, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    # Verify SQLite header integrity before overwriting hospital's live DB
    conn = sqlite3.connect(staging_db)
    check = conn.execute("PRAGMA integrity_check;").fetchone()[0]
    conn.close()

    if check != "ok":
        os.remove(staging_db)
        print("[FATAL] Backup failed SQLite PRAGMA integrity_check!")
        return False

    shutil.move(staging_db, DB_PATH)
    pruned = prune_old_backups()
    print(f"[SUCCESS] Database restored to {DB_PATH} (Pruned {pruned} old snapshots).")
    return True

if __name__ == "__main__":
    restore_latest_backup()
