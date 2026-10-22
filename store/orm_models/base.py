"""ORM 基类（所有模型共享同一 DeclarativeBase / metadata）。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
