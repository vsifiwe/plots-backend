import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def copy_database(source, target):
    # Opening the source read-only prevents a missing database from being created.
    source_uri = f"{source.as_uri()}?mode=ro"
    with target.open("xb"), closing(sqlite3.connect(source_uri, uri=True)) as original:
        with closing(sqlite3.connect(target)) as backup:
            original.backup(backup)


if __name__ == "__main__":
    copy_database(Path(sys.argv[1]), Path(sys.argv[2]))
