#!/usr/bin/env python3
# init_db.py

from sqlalchemy import create_engine

from models import Base


DATABASE_URL = "sqlite:///db.sqlite3"


def main():
    engine = create_engine(DATABASE_URL)

    Base.metadata.create_all(engine)

    print("Created db.sqlite3")


if __name__ == "__main__":
    main()
