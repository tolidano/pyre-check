#!/usr/bin/env python3

import logging
from collections import namedtuple
from itertools import islice, tee
from typing import Any, Dict, List, Optional, Set, Tuple, Type

from munch import Munch
from sapp.errors import AIException
from sapp.iterutil import split_every
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    exc,
    func,
    inspect,
    or_,
    types,
)
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.dialects.mysql import BIGINT, INTEGER
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, relationship


log = logging.getLogger()

Base = declarative_base()
INNODB_MAX_INDEX_LENGTH = 767
HANDLE_LENGTH = 255
MESSAGE_LENGTH = 4096
SHARED_TEXT_LENGTH = 4096

"""Number of variables that can safely be set on a single DB call"""
BATCH_SIZE = 900

"""Models used to represent DB entries

An Issue is a particular problem found. It can exist across multiple commits.  A
Run is a single run of Zoncolan over a specific commit. It may find new Issues,
or existing Issues.  Each run is tied to Issues through IssueInstances.
IssueInstances have per run information, like source location, while Issues have
attributes like the status of an issue.
"""

"""Tables that should be removed from existing databases.

These are tables that are NO LONGER in use, and will be removed. Don't add
tables here until all code that was using them has been updated.
"""
PurgeMetadata = MetaData()
for t in [
    "active_jobs",
    "deletion_history",
    "herald_task_history",
    "precondition_incremental_lookup",
    "run_precondition_assoc",
    "run_postcondition_assoc",
]:
    Table(t, PurgeMetadata)


class PrepareMixin(object):
    @classmethod
    def prepare(cls, session, pkgen, items):
        """This is called immediately before the items are written to the
        database. pkgen is passed in to allow last-minute resolving of ids.
        """
        for item in cls.merge(session, items):
            if hasattr(item, "id"):
                item.id.resolve(id=pkgen.get(cls), is_new=True)
            yield cls.to_dict(item)

    @classmethod
    def merge(cls, session, items):
        """Models should override this to perform a merge"""
        return items

    @classmethod
    def _merge_by_key(cls, session, items, attr):
        return cls._merge_by_keys(
            session, items, lambda item: getattr(item, attr.key), attr
        )

    @classmethod
    def _merge_by_keys(cls, session, items, hash_item, *attrs):
        """An object can have multiple attributes as its key. This merges the
        items to be added with existing items in the database based on their
        key(s).

        session: Session object for querying the DB.
        items: Iterator of items to be added to the DB.
        hash_item: Function that takes as in put the item to be added and
                   returns a hash of it.
        attrs: List of attributes of the object/class that represent the
               object's key.

        Returns the next item (in items) that is not already in the DB.
        """
        # Note: items is an iterator, not an iterable, 'tee' is a must.
        items_iter1, items_iter2 = tee(items)

        keys = {}  # map of hash -> keys of the item
        for i in items_iter1:
            # An item's key is a map of 'attr -> item[attr]' where attr is
            # usually a column name.
            # For 'SharedText', its key would look like: {
            #   "kind": "feature",
            #   "contents": "via tito",
            # }
            item_hash = hash_item(i)
            keys[item_hash] = {attr.key: getattr(i, attr.key) for attr in attrs}

        # Find existing items.
        existing_ids = {}  # map of item_hash -> existing ID
        cls_attrs = [getattr(cls, attr.key) for attr in attrs]
        for fetch_keys in split_every(BATCH_SIZE, keys.values()):
            filters = []
            for fetch_key in fetch_keys:
                # Sub-filters for checking if item with fetch_key is in the DB
                # Example: [
                #   SharedText.kind.__eq__("feature"),
                #   SharedText.contents.__eq__("via tito"),
                # ]
                subfilter = [
                    getattr(cls, attr).__eq__(val) for attr, val in fetch_key.items()
                ]
                filters.append(and_(*subfilter))
            existing_items = (
                session.query(cls.id, *cls_attrs).filter(or_(*(filters))).all()
            )
            for existing_item in existing_items:
                item_hash = hash_item(existing_item)
                existing_ids[item_hash] = existing_item.id

        # Now see if we can merge
        new_items = {}
        for i in items_iter2:
            item_hash = hash_item(i)
            if item_hash in existing_ids:
                # The key is already in the DB
                i.id.resolve(existing_ids[item_hash], is_new=False)
            elif item_hash in new_items:
                # The key is already in the list of new items
                i.id.resolve(new_items[item_hash].id, is_new=False)
            else:
                # The key is new
                new_items[item_hash] = i
                yield i

    @classmethod
    def _merge_assocs(cls, session, items, id1, id2):
        new_items = {}
        for i in items:
            r1 = getattr(i, id1.key)
            r2 = getattr(i, id2.key)
            key = (r1.resolved(), r2.resolved())
            if key not in new_items:
                new_items[key] = i
                yield i


# The record mixin class is more efficient than the MutableRecordMixin, so it
# should be preferred. But the performance isn't from the mutability, it's
# because we use namedtuples, which creates a new class on demand, which uses
# __slots__, which is more efficient. Both of these mixins can be replaced when
# we have dynamically created classes with the slots set. But until then,
# prefer RecordMixin unless you need to change fields after creation.
class RecordMixin(object):
    _record = None

    @classmethod
    def Record(cls, extra_fields=None, **kwargs):
        if not cls._record:
            if not extra_fields:
                extra_fields = []
            mapper = inspect(cls)
            keys = [c.key for c in mapper.column_attrs] + ["model"] + extra_fields
            cls._record = namedtuple(cls.__name__ + "Record", keys)

        return cls._record(model=cls, **kwargs)

    @classmethod
    def to_dict(cls, obj):
        return obj._asdict()


class MutableRecordMixin(object):
    @classmethod
    def Record(cls, **kwargs):
        return Munch(model=cls, **kwargs)

    @classmethod
    def to_dict(cls, obj):
        return obj.toDict()


class SourceLocation(object):
    """The location in a source file that an error occurred in

    If end_column is defined then we have a range, otherwise it defaults to
    begin_column and we have a single point.
    """

    def __init__(self, line_no, begin_column, end_column=None):
        self.line_no = line_no
        self.begin_column = begin_column
        self.end_column = end_column or self.begin_column

    def __eq__(self, other):
        return (
            self.line_no == other.line_no
            and self.begin_column == other.begin_column
            and self.end_column == other.end_column
        )

    @staticmethod
    def from_string(location_string):
        location_points = location_string.split("|")
        assert len(location_points) == 3, "Invalid location string %s" % location_string
        return SourceLocation(*location_points)

    @staticmethod
    def to_string(location):
        return "|".join(
            map(str, [location.line_no, location.begin_column, location.end_column])
        )


class CaseSensitiveStringType(types.TypeDecorator):
    impl = types.String

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(
                mysql.VARCHAR(length=255, collation="latin1_general_cs")
            )
        elif dialect.name == "sqlite":
            return dialect.type_descriptor(
                sqlite.VARCHAR(length=255, collation="binary")
            )
        else:
            raise AIException("%s not supported" % dialect.name)


class SourceLocationType(types.TypeDecorator):
    """Defines a new type of SQLAlchemy to store source locations.

    In python land we use SourceLocation, but when stored in the databae we just
    split the fields with |
    """

    impl = types.String

    def __init__(self):
        super(SourceLocationType, self).__init__(length=255)

    def process_bind_param(self, value, dialect):
        """
        SQLAlchemy uses this to convert a SourceLocation object into a string.
        """
        if value is None:
            return None
        return SourceLocation.to_string(value)

    def process_result_value(self, value, dialect):
        """
        SQLAlchemy uses this to convert a string into a SourceLocation object.
        We separate the fields by a |
        """
        if value is None:
            return None

        p = value.split("|")

        if len(p) == 0:
            return None
        return SourceLocation(*map(int, p))


class SourceLocationsType(types.TypeDecorator):
    """Defines a type to store multiple source locations in a single string"""

    impl = types.String

    def __init__(self):
        super(SourceLocationsType, self).__init__(length=4096)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return ",".join([SourceLocation.to_string(l) for l in value])

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return []
        assert isinstance(value, str), "Invalid SourceLocationsType %s" % str(value)
        locations = value.split(",")
        return [SourceLocation.from_string(location) for location in locations]


# The following three DBID classes require some explanation. Normally models
# will reference each other by their id. But we do bulk insertion at the end
# of our processing, which means the id isn't set until later. Having a DBID
# object allows these models to reference each other before that point. When
# we are ready to insert into the database, PrimaryKeyGenerator will give it
# an ID. Any other models referencing that DBID object will now be able to use
# the real id.


class DBID(object):
    __slots__ = ["_id", "is_new", "local_id"]

    # Temporary IDs that are local per run (local_id) are assigned for each
    # DBID object on creation. This acts as a key for the object in map-like
    # structures of DB objects without having to define a hashing function for
    # each of them. next_id tracks the next available int to act as an id.
    next_id: int = 0

    def __init__(self, id=None):
        self.resolve(id)
        self.local_id: int = DBID.next_id
        DBID.next_id += 1

    def resolve(self, id, is_new=True):
        self._check_type(id)
        self._id = id
        self.is_new = is_new
        return self

    def resolved(self):
        id = self._id

        # We allow one level of a DBID pointing to another DBID
        if isinstance(id, DBID):
            id = id.resolved()

        return id

    def _check_type(self, id):
        if not isinstance(id, (int, type(None), DBID)):
            raise TypeError(
                "id expected to be type '{}' but was type '{}'".format(int, type(id))
            )

    # Allow DBIDs to be added and compared as ints
    def __int__(self):
        return self.resolved()

    def __str__(self):
        return str(self.resolved())

    def __add__(self, other):
        return int(self) + int(other)

    def __lt__(self, other):
        return int(self) < int(other)

    def __gt__(self, other):
        return int(self) > int(other)

    def __ge__(self, other):
        return int(self) >= int(other)

    def __le__(self, other):
        return int(self) <= int(other)

    def __repr__(self):
        return "<{}(id={}) object at 0x{:x}>".format(
            self.__class__.__name__, self._id, id(self)
        )


class DBIDType(types.TypeDecorator):
    impl = types.Integer

    def process_bind_param(self, value, dialect):
        # If it is a DBID wrapper, then write the contained value. Otherwise it
        # may be resolved already, or None.
        if isinstance(value, DBID):
            return value.resolved()
        else:
            return value

    def process_result_value(self, value, dialect):
        return DBID(value)

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.INTEGER(unsigned=True))
        return self.impl


class BIGDBIDType(DBIDType):
    impl = types.BigInteger

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.BIGINT(unsigned=True))
        return self.impl


# See Issue.merge for information about replace_assocs


class IssueDBID(DBID):
    __slots__ = ["replace_assocs"]

    def __init__(self, id=None):
        super().__init__(id)
        self.replace_assocs = False


class IssueDBIDType(DBIDType):
    def process_result_value(self, value, dialect):
        return IssueDBID(value)


class IssueBIGDBIDType(BIGDBIDType):
    def process_result_value(self, value, dialect):
        return IssueDBID(value)


class IssueInstanceTraceFrameAssoc(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "issue_instance_trace_frame_assoc"

    issue_instance_id = Column(
        "issue_instance_id", BIGDBIDType, primary_key=True, nullable=False
    )

    trace_frame_id = Column(
        "trace_frame_id", BIGDBIDType, primary_key=True, nullable=False, index=True
    )

    issue_instance = relationship(
        "IssueInstance",
        primaryjoin=(
            "IssueInstanceTraceFrameAssoc.issue_instance_id == "
            "foreign(IssueInstance.id)"
        ),
        uselist=False,
    )

    trace_frame = relationship(
        "TraceFrame",
        primaryjoin=(
            "IssueInstanceTraceFrameAssoc.trace_frame_id == " "foreign(TraceFrame.id)"
        ),
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(
            session, items, cls.issue_instance_id, cls.trace_frame_id
        )


class IssueInstancePostconditionAssoc(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "issue_instance_postcondition_assoc"

    issue_instance_id = Column(
        "issue_instance_id", BIGDBIDType, primary_key=True, nullable=False
    )

    postcondition_id = Column(
        "postcondition_id", BIGDBIDType, primary_key=True, nullable=False, index=True
    )

    issue_instance = relationship(
        "IssueInstance",
        primaryjoin=(
            "IssueInstancePostconditionAssoc.issue_instance_id == "
            "foreign(IssueInstance.id)"
        ),
        uselist=False,
    )

    postcondition = relationship(
        "Postcondition",
        primaryjoin=(
            "IssueInstancePostconditionAssoc.postcondition_id == "
            "foreign(Postcondition.id)"
        ),
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(
            session, items, cls.issue_instance_id, cls.postcondition_id
        )


class IssueInstancePreconditionAssoc(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "issue_instance_precondition_assoc"

    issue_instance_id = Column(
        "issue_instance_id", BIGDBIDType, primary_key=True, nullable=False
    )

    precondition_id = Column(
        "precondition_id", BIGDBIDType, nullable=False, primary_key=True, index=True
    )

    issue_instance = relationship(
        "IssueInstance",
        primaryjoin=(
            "IssueInstancePreconditionAssoc.issue_instance_id == "
            "foreign(IssueInstance.id)"
        ),
        uselist=False,
    )

    precondition = relationship(
        "Precondition",
        primaryjoin=(
            "IssueInstancePreconditionAssoc.precondition_id == "
            "foreign(Precondition.id)"
        ),
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(
            session, items, cls.issue_instance_id, cls.precondition_id
        )


class SharedTextKind(Enum):
    FEATURE = "feature"
    MESSAGE = "message"
    SOURCE = "source"
    SINK = "sink"


class SharedText(Base, PrepareMixin, RecordMixin):  # noqa
    """Any string-ish type that can be shared as a property of some other
    object. (e.g. features, sources, sinks). The table name 'messages' is due
    to legacy reasons."""

    __tablename__ = "messages"

    __table_args__ = (Index("ix_messages_handle", "contents", "kind"),)

    id: DBID = Column(BIGDBIDType, primary_key=True)

    contents: str = Column(
        String(length=SHARED_TEXT_LENGTH), nullable=False, index=True
    )

    kind: SharedTextKind = Column(
        Enum(
            SharedTextKind.FEATURE,
            SharedTextKind.MESSAGE,
            SharedTextKind.SOURCE,
            SharedTextKind.SINK,
        ),
        server_default=SharedTextKind.FEATURE,
        nullable=False,
        index=True,
    )

    issue_instances = association_proxy("shared_text_issue_instance", "issue_instance")

    shared_text_issue_instance = relationship(
        "IssueInstanceSharedTextAssoc",
        primaryjoin=(
            "SharedText.id == " "foreign(IssueInstanceSharedTextAssoc.shared_text_id)"
        ),
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_by_keys(
            session,
            items,
            lambda item: "%s:%s" % (item.contents, item.kind),
            cls.contents,
            cls.kind,
        )


class IssueInstanceSharedTextAssoc(Base, PrepareMixin, RecordMixin):  # noqa
    """Assoc table between issue instances and its properties that are
    representable by a string. The DB table name and column names are due to
    legacy reasons and warrant some explanation:
    - 'Features' used to be the only shared text of the assoc, now, the assoc
      also accounts for 'Sources' and 'Sinks' and possibly more.
    - 'messages' table used to be only for 'messages', now, it contains
      features, sources and sinks and possibly more.
    - It is expensive to rename the DB tables, so renaming only happened in
      the model. This is why it looks like we have 3 different terms for the
      same thing: 'messages', 'shared_text', 'features'.

    When in doubt, trust the property and method names used in the model and
    refer to the relationship joins for how objects relate to each other.
    """

    __tablename__ = "issue_instance_feature_assoc"

    issue_instance_id = Column(
        "issue_instance_id", BIGDBIDType, primary_key=True, nullable=False
    )

    shared_text_id = Column("feature_id", BIGDBIDType, primary_key=True, nullable=False)

    issue_instance = relationship(
        "IssueInstance",
        primaryjoin=(
            "IssueInstanceSharedTextAssoc.issue_instance_id =="
            "foreign(IssueInstance.id)"
        ),
        uselist=False,
    )

    shared_text = relationship(
        "SharedText",
        primaryjoin=(
            "IssueInstanceSharedTextAssoc.shared_text_id == " "foreign(SharedText.id)"
        ),
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(
            session, items, cls.issue_instance_id, cls.shared_text_id
        )


class IssueInstance(Base, PrepareMixin, MutableRecordMixin):  # noqa
    """A particularly instance of an issue found in a run"""

    __tablename__ = "issue_instances"

    id: DBID = Column(BIGDBIDType, primary_key=True)

    location = Column(
        SourceLocationType,
        nullable=False,
        doc="Location (possibly a range) of the issue",
    )

    filename: str = Column(
        String(length=767),
        doc="Filename containing the issue",
        nullable=True,
        index=True,
    )

    taint_locations = Column(
        SourceLocationsType,
        nullable=True,
        doc="Locations with interesting taint information",
    )

    is_new_issue = Column(
        Boolean,
        index=True,
        default=False,
        doc="True if the issue did not exist before this instance",
    )

    run_id = Column(BIGDBIDType, nullable=False, index=True)

    issue_id = Column(BIGDBIDType, nullable=False, index=True)

    fix_info_id = Column(BIGDBIDType, nullable=True)

    fix_info = relationship(
        "IssueInstanceFixInfo",
        primaryjoin=(
            "foreign(IssueInstanceFixInfo.id) == " "IssueInstance.fix_info_id"
        ),
        uselist=False,
    )

    message_id = Column(BIGDBIDType, nullable=True)

    message = relationship(
        "SharedText",
        primaryjoin="foreign(SharedText.id) == IssueInstance.message_id",
        uselist=False,
    )

    preconditions = association_proxy("issue_instance_precondition", "precondition")

    issue_instance_precondition = relationship(
        "IssueInstancePreconditionAssoc",
        primaryjoin=(
            "IssueInstance.id == "
            "foreign(IssueInstancePreconditionAssoc.issue_instance_id)"
        ),
    )

    postconditions = association_proxy("issue_instance_postcondition", "postcondition")

    issue_instance_postcondition = relationship(
        "IssueInstancePostconditionAssoc",
        primaryjoin=(
            "IssueInstance.id == "
            "foreign(IssueInstancePostconditionAssoc.issue_instance_id)"
        ),
    )

    shared_texts = association_proxy("issue_instance_shared_text", "shared_text")

    issue_instance_shared_text = relationship(
        "IssueInstanceSharedTextAssoc",
        primaryjoin=(
            "IssueInstance.id == "
            "foreign(IssueInstanceSharedTextAssoc.issue_instance_id)"
        ),
    )

    min_trace_length_to_sources = Column(
        Integer, nullable=True, doc="The minimum trace length to sources"
    )

    min_trace_length_to_sinks = Column(
        Integer, nullable=True, doc="The minimum trace length to sinks"
    )

    rank = Column(
        Integer,
        server_default="0",
        doc="The higher the rank, the higher the priority for this issue",
    )

    callable_count = Column(
        Integer,
        server_default="0",
        doc="Number of issues in this callable for this run",
    )

    def get_shared_texts_by_kind(self, kind: SharedTextKind):
        return [text for text in self.shared_texts if text.kind == kind]

    @classmethod
    def merge(cls, session, items):
        for i in items:
            # If the issue is new, then the instance has to be new. But note
            # that we still may need RunDiffer, because issues that disappeared
            # for a while and then came back are also marked new.
            i.is_new_issue = i.issue_id.is_new
            yield i


class IssueStatus(Enum):
    """Issues are born uncategorized. Humans can
    set it to FALSE_POSITIVE or VALID_BUG upon review."""

    """Not a security bug, but a bad practice. Still needs fixing."""
    BAD_PRACTICE = "bad_practice"
    """False positive from analysis"""
    FALSE_POSITIVE = "false_positive"
    """Reviewed and seen to be a valid bug that needs fixing"""
    VALID_BUG = "valid_bug"
    """An issue that hasn't been marked as a bug or FP"""
    UNCATEGORIZED = "uncategorized"
    """I don't care about this particular issue,
    but still want to see issues of this kind."""
    DO_NOT_CARE = "do_not_care"


class Issue(Base, PrepareMixin, MutableRecordMixin):  # noqa
    """An issue coming from the static analysis.

    An issue can persist across multiple runs, even if it moves around in the
    code.
    """

    __tablename__ = "issues"

    id: IssueDBID = Column(IssueBIGDBIDType, primary_key=True, nullable=False)

    handle = Column(
        String(length=HANDLE_LENGTH),
        nullable=False,
        unique=True,
        doc="This handle should uniquely identify an issue across runs on "
        + "different code revisions",
    )

    message__DEPRECATED = Column(
        "message",
        String(length=MESSAGE_LENGTH),
        doc="Deprecated: Use IssueInstance.message instead",
        nullable=True,
    )

    code = Column(
        Integer, doc="Code identifiying the issue type", nullable=False, index=True
    )

    filename: str = Column(
        String(length=767),
        doc="Filename containing the issue",
        nullable=True,
        index=True,
    )

    callable = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        doc="Callable containing the issue",
        nullable=False,
        index=True,
    )

    instances = relationship(
        "IssueInstance",
        primaryjoin="Issue.id == foreign(IssueInstance.issue_id)",
        backref="issue",
    )

    first_seen = Column(
        DateTime,
        doc="time of the first run that found this issue",
        nullable=False,
        index=True,
    )

    last_seen_DEPRECATED = Column(
        "last_seen",
        DateTime,
        doc="Deprecated. Time of most recent run that found this issue",
        nullable=True,
        index=True,
    )

    run_id = Column("run_id", BIGDBIDType, nullable=True, index=True)

    triage_info_assoc = relationship(
        "IssueTriageInfoAssoc",
        primaryjoin="Issue.id == foreign(IssueTriageInfoAssoc.issue_id)",
    )

    json = Column(types.TEXT, doc="Raw JSON of original issue", nullable=True)

    @classmethod
    def _take(cls, n, iterable):
        "Return first n items of the iterable as a list"
        return list(islice(iterable, n))

    @classmethod
    def merge(cls, session, issues):
        attr = cls.handle
        new_issues = {}
        existing_ids = {}
        issues_iter1, issues_iter2 = tee(issues)
        attr_key = attr.key
        all_keys = {getattr(i, attr_key) for i in issues_iter1}

        # First, see if any of these keys exist in the database already
        cls_attr = getattr(cls, attr.key)
        for fetch_keys in split_every(BATCH_SIZE, all_keys):
            for key, id in (
                session.query(cls_attr, cls.id).filter(cls_attr.in_(fetch_keys)).all()
            ):
                existing_ids[key] = id

        # Now see if we can merge
        for i in issues_iter2:
            key = getattr(i, attr_key)
            if key in new_issues:
                # We don't expect the exact same issue to show up twice, so
                # lets warn about it. This must be done before the existing
                # issues check in order to warn on duplicates within the run.
                log.warning("Same issue (handle=%s) showed up twice in a run", i.handle)
                i.id.resolve(new_issues[key].id, is_new=False)
            elif key in existing_ids:
                orig_id = existing_ids[key]
                # The key is already in the DB
                i.id.resolve(orig_id, is_new=False)
            else:
                # The key is new
                new_issues[key] = i
                yield i


class RunStatus(Enum):
    FINISHED = "finished"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"
    FAILED = "failed"


class Run(Base):  # noqa
    """A particular run of the static analyzer.

    Each time output is parsed from the static analyzer we generate a new run. A
    run has multiple IssueInstances."""

    __tablename__ = "runs"

    id = Column(BIGDBIDType, primary_key=True)

    job_id = Column(String(length=255), unique=True)

    date = Column(DateTime, doc="The date/time the analysis was run", nullable=False)

    commit_hash = Column(
        String(length=255),
        doc="The commit hash of the codebase",
        nullable=True,
        index=True,
    )

    revision_id = Column(
        Integer, doc="Differential revision (DXXXXXX)", nullable=True, index=True
    )

    differential_id = Column(
        Integer,
        doc="Differential diff (instance of revision)",
        nullable=True,
        index=True,
    )

    hh_version = Column(String(length=255), doc="The output of hh_server --version")

    branch = Column(
        String(length=255),
        doc="Branch the commit is based on",
        nullable=True,
        index=True,
    )

    issue_instances = relationship(
        "IssueInstance",
        primaryjoin="Run.id == foreign(IssueInstance.run_id)",
        backref="run",
    )

    status = Column(
        Enum(
            RunStatus.FINISHED,
            RunStatus.INCOMPLETE,
            RunStatus.SKIPPED,
            RunStatus.FAILED,
            name="run_states",
        ),
        server_default=RunStatus.FINISHED,
        nullable=False,
        index=True,
    )

    status_description = Column(
        String(length=255), doc="The reason why a run didn't finish", nullable=True
    )

    previous_run_id = Column(BIGDBIDType, nullable=True, index=True)

    previous_run = relationship(
        "Run",
        uselist=False,
        primaryjoin="(remote(Run.id) == foreign(Run.previous_run_id))",
    )

    kind = Column(
        String(length=255),
        doc=(
            "Specify different kinds of runs, e.g. MASTER vs. TEST., GKFORXXX, etc. "
            "in the same DB"
        ),
        nullable=True,
        index=True,
    )

    repository = Column(
        String(length=255),
        doc=("The repository that static analysis was run on."),
        nullable=True,
    )

    def get_summary(self, **kwargs):
        session = Session.object_session(self)

        return RunSummary(
            commit_hash=self.commit_hash,
            differential_id=self.differential_id,
            id=self.id.resolved(),
            job_id=self.job_id,
            num_new_issues=self._get_num_new_issue_instances(session),
            num_total_issues=self._get_num_total_issues(session),
            alarm_counts=self._get_alarm_counts(session),
        )

    def new_issue_instances(self):
        session = Session.object_session(self)
        return (
            session.query(IssueInstance)
            .filter(IssueInstance.run_id == self.id)
            .filter(IssueInstance.is_new_issue.is_(True))
            .all()
        )

    def _get_num_new_issue_instances(self, session):
        return (
            session.query(IssueInstance)
            .filter(IssueInstance.run_id == self.id)
            .filter(IssueInstance.is_new_issue.is_(True))
            .count()
        )

    def _get_num_total_issues(self, session):
        return (
            session.query(IssueInstance).filter(IssueInstance.run_id == self.id).count()
        )

    def _get_alarm_counts(self, session):
        return dict(
            session.query(Issue.code, func.count(Issue.code))
            .filter(IssueInstance.run_id == self.id)
            .outerjoin(IssueInstance.issue)
            .group_by(Issue.code)
            .all()
        )


class RunSummary:
    def __init__(
        self,
        commit_hash,
        differential_id,
        id,
        job_id,
        num_new_issues,
        num_total_issues,
        num_missing_preconditions=-1,
        num_missing_postconditions=-1,
        alarm_counts=None,
    ):
        self.commit_hash = commit_hash
        self.differential_id = differential_id
        self.id = id
        self.job_id = job_id
        self.num_new_issues = num_new_issues
        self.num_total_issues = num_total_issues
        self.num_missing_preconditions = num_missing_preconditions
        self.num_missing_postconditions = num_missing_postconditions
        self.alarm_counts = alarm_counts or {}

    def todict(self) -> Dict[str, Any]:
        return self.__dict__

    @classmethod
    def fromdict(cls, d):
        return cls(**d)


class TraceFrameLeafAssoc(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "trace_frame_message_assoc"

    trace_frame_id = Column(BIGDBIDType, nullable=False, primary_key=True)

    leaf_id = Column("message_id", BIGDBIDType, nullable=False, primary_key=True)

    trace_length = Column(
        Integer, doc="minimum trace length to the given leaf", nullable=True
    )

    trace_frame = relationship(
        "TraceFrame",
        primaryjoin=("TraceFrameLeafAssoc.trace_frame_id == " "foreign(TraceFrame.id)"),
        uselist=False,
    )

    leaves = relationship(
        "SharedText",
        primaryjoin="TraceFrameLeafAssoc.leaf_id == foreign(SharedText.id)",
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(session, items, cls.trace_frame_id, cls.leaf_id)


class Sink(Base, PrepareMixin, RecordMixin):  # noqa
    """Defines a sink for the analysis"""

    __tablename__ = "sinks"

    id: DBID = Column(BIGDBIDType, primary_key=True)

    name: str = Column(CaseSensitiveStringType(), nullable=False, unique=True)

    preconditions = association_proxy("sink_precondition", "precondition")

    sink_precondition = relationship(
        "PreconditionSinkAssoc",
        primaryjoin="Sink.id == foreign(PreconditionSinkAssoc.sink_id)",
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_by_key(session, items, cls.name)


class Source(Base, PrepareMixin, RecordMixin):  # noqa
    """Defines a source for the analysis"""

    __tablename__ = "sources"

    id: DBID = Column(BIGDBIDType, primary_key=True)

    name: str = Column(CaseSensitiveStringType(), nullable=False, unique=True)

    postconditions = association_proxy("source_postcondition", "postcondition")

    source_postcondition = relationship(
        "PostconditionSourceAssoc",
        primaryjoin="Source.id == foreign(PostconditionSourceAssoc.source_id)",
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_by_key(session, items, cls.name)


class PostconditionSourceAssoc(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "postcondition_source_assoc"

    postcondition_id = Column(BIGDBIDType, nullable=False, primary_key=True)

    source_id = Column(BIGDBIDType, nullable=False, primary_key=True)

    trace_length = Column(
        Integer, doc="minimum trace length to given source", nullable=True
    )

    postcondition = relationship(
        "Postcondition",
        primaryjoin=(
            "PostconditionSourceAssoc.postcondition_id == " "foreign(Postcondition.id)"
        ),
        uselist=False,
    )

    source = relationship(
        "Source",
        primaryjoin="PostconditionSourceAssoc.source_id == foreign(Source.id)",
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(session, items, cls.postcondition_id, cls.source_id)


class PreconditionSinkAssoc(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "precondition_sink_assoc"

    precondition_id = Column(BIGDBIDType, nullable=False, primary_key=True)

    sink_id = Column(BIGDBIDType, nullable=False, primary_key=True)

    trace_length = Column(
        Integer, doc="minimum trace length to given source", nullable=True
    )

    precondition = relationship(
        "Precondition",
        primaryjoin=(
            "PreconditionSinkAssoc.precondition_id == " "foreign(Precondition.id)"
        ),
        uselist=False,
    )

    sink = relationship(
        "Sink",
        primaryjoin="PreconditionSinkAssoc.sink_id == foreign(Sink.id)",
        uselist=False,
    )

    @classmethod
    def merge(cls, session, items):
        return cls._merge_assocs(session, items, cls.precondition_id, cls.sink_id)


class IssueInstanceFixInfo(Base, PrepareMixin, RecordMixin):  # noqa
    __tablename__ = "issue_instance_fix_info"

    id: DBID = Column(BIGDBIDType, nullable=False, primary_key=True)

    fix_info = Column(String(length=INNODB_MAX_INDEX_LENGTH), nullable=False)

    issue_instance = relationship(
        "IssueInstance",
        primaryjoin=(
            "foreign(IssueInstance.fix_info_id) == " "IssueInstanceFixInfo.id"
        ),
        uselist=False,
    )


class TraceKind(Enum):
    PRECONDITION = "precondition"
    POSTCONDITION = "postcondition"


class TraceFrame(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "trace_frames"

    __table_args__ = (
        Index("ix_traceframe_caller", "caller"),
        Index("ix_traceframe_caller_and_port", "caller", "caller_port"),
    )

    id: DBID = Column(BIGDBIDType, nullable=False, primary_key=True)

    kind = Column(
        Enum(TraceKind.PRECONDITION, TraceKind.POSTCONDITION),
        nullable=False,
        index=True,
    )

    caller: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc="The function/method that produces the tainted trace",
    )

    caller_port: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        server_default="",
        doc="The caller port of this call edge",
    )

    callee: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc="The function/method within the caller that produces the tainted trace.",
    )

    callee_location = Column(
        SourceLocationType,
        nullable=False,
        doc="The location of the callee in the source code (line|start|end)",
    )

    callee_port: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        server_default="",
        doc="The callee port of this call edge'",
    )

    filename: str = Column(
        String(length=4096), doc="Filename containing the call", nullable=False
    )

    run_id = Column("run_id", BIGDBIDType, nullable=False, index=True)

    type_interval_lower = Column(
        Integer, nullable=True, doc="Class interval lower-bound (inclusive)"
    )

    type_interval_upper = Column(
        Integer, nullable=True, doc="Class interval upper-bound (inclusive)"
    )

    preserves_type_context = Column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
        doc="Whether the call preserves the calling type context",
    )

    titos = Column(
        SourceLocationsType,
        doc="Locations of TITOs aka abductions for the trace frame",
        nullable=False,
        server_default="",
    )

    annotations = relationship(
        "TraceFrameAnnotation",
        primaryjoin=(
            "TraceFrame.id == " "foreign(TraceFrameAnnotation.trace_frame_id)"
        ),
        uselist=True,
    )

    leaves = association_proxy("trace_frame_messages", "messages")

    leaf_assoc = relationship(
        "TraceFrameLeafAssoc",
        primaryjoin=("TraceFrame.id == " "foreign(TraceFrameLeafAssoc.trace_frame_id)"),
    )

    issue_instances = association_proxy("trace_frame_issue_instance", "issue_instance")

    trace_frame_issue_instance = relationship(
        "IssueInstanceTraceFrameAssoc",
        primaryjoin=(
            "TraceFrame.id == " "foreign(IssueInstanceTraceFrameAssoc.trace_frame_id)"
        ),
    )


class Postcondition(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "postconditions"

    __table_args__ = (Index("ix_caller", "caller"),)

    id: DBID = Column(BIGDBIDType, nullable=False, primary_key=True)

    caller: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc="The function/method that produces tainted postcondition(s)",
    )

    callee: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc="The function/method within the caller that produces tainted "
        "postcondition(s). Same as the caller if this is a Source.",
    )

    callee_location = Column(
        SourceLocationType,
        nullable=False,
        doc="The location the callee in the source code (line|start|end)",
    )

    filename: str = Column(
        String(length=4096), doc="Filename containing the call", nullable=False
    )

    sources = association_proxy("postcondition_source", "source")

    postcondition_source = relationship(
        "PostconditionSourceAssoc",
        primaryjoin=(
            "Postcondition.id == " "foreign(PostconditionSourceAssoc.postcondition_id)"
        ),
    )

    run_id = Column("run_id", BIGDBIDType, nullable=True, index=True)

    issue_instances = association_proxy(
        "postcondition_issue_instance", "issue_instance"
    )

    postcondition_issue_instance = relationship(
        "IssueInstancePostconditionAssoc",
        primaryjoin=(
            "Postcondition.id == "
            "foreign(IssueInstancePostconditionAssoc.postcondition_id)"
        ),
    )

    caller_condition: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        doc="The caller port of this call edge",
        nullable=False,
        server_default="",
    )

    callee_condition: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        doc="The callee port of this call edge",
        nullable=False,
        server_default="",
    )

    type_interval_lower = Column(
        Integer, doc="Class interval lower-bound (inclusive)", nullable=True
    )

    type_interval_upper = Column(
        Integer, doc="Class interval upper-bound (inclusive)", nullable=True
    )

    preserves_type_context = Column(
        Boolean,
        doc="Whether the call preserves calling type context.",
        default=False,
        server_default="0",
        nullable=False,
    )


class Precondition(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "preconditions"

    __table_args__ = (Index("ix_caller_and_condition", "caller", "caller_condition"),)

    id: DBID = Column(BIGDBIDType, nullable=False, primary_key=True)

    caller: str = Column(String(length=INNODB_MAX_INDEX_LENGTH), nullable=False)

    caller_condition: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc=(
            "The condition that must be true for the "
            "caller for the callee to be interesting"
        ),
    )

    callee: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc="The call within the callable name",
    )

    callee_condition: str = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=False,
        doc="The condition that must match to proceed to the next call",
    )

    callee_location = Column(
        SourceLocationType, nullable=False, doc="The location the call"
    )

    filename: str = Column(
        String(length=4096), doc="Filename containing the call", nullable=False
    )

    message = Column(
        String(length=4096),
        doc="Message describing why this precondition is interesting",
        nullable=False,
    )

    sinks = association_proxy("precondition_sink", "sink")

    precondition_sink = relationship(
        "PreconditionSinkAssoc",
        primaryjoin=(
            "Precondition.id == " "foreign(PreconditionSinkAssoc.precondition_id)"
        ),
    )

    run_id = Column("run_id", BIGDBIDType, nullable=True, index=True)

    issue_instances = association_proxy("precondition_issue_instance", "issue_instance")

    precondition_issue_instance = relationship(
        "IssueInstancePreconditionAssoc",
        primaryjoin=(
            "Precondition.id == "
            "foreign(IssueInstancePreconditionAssoc.precondition_id)"
        ),
    )

    titos = Column(
        SourceLocationsType,
        doc="Locations of TITOs aka abductions for the precondition",
        nullable=False,
        server_default="",
    )

    type_interval_lower = Column(
        Integer, doc="Class interval lower-bound (inclusive)", nullable=True
    )

    type_interval_upper = Column(
        Integer, doc="Class interval upper-bound (inclusive)", nullable=True
    )

    preserves_type_context = Column(
        Boolean,
        doc="Whether the call preserves calling type context.",
        default=False,
        server_default="0",
        nullable=False,
    )

    annotations = relationship(
        "TraceFrameAnnotation",
        primaryjoin=(
            "Precondition.id == " "foreign(TraceFrameAnnotation.trace_frame_id)"
        ),
        uselist=True,
    )


# Extra bits of information we can show on a TraceFrame.
class TraceFrameAnnotation(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "trace_frame_annotations"

    id: DBID = Column(BIGDBIDType, nullable=False, primary_key=True)

    location = Column(
        SourceLocationType, nullable=False, doc="The location for the message"
    )

    message: str = Column(
        String(length=4096),
        doc="Message describing info about the trace",
        nullable=False,
    )

    link: Optional[str] = Column(
        String(length=4096),
        doc="An optional URL linking the message to more info (Quandary)",
        nullable=True,
    )

    trace_key: Optional[str] = Column(
        String(length=INNODB_MAX_INDEX_LENGTH),
        nullable=True,
        doc="Link to possible pre/post traces (caller_condition).",
    )

    # For now we have relationships with both TraceFrame and Precondition.
    # trace_frame_id and trace_frame are historically connected to a Precondition
    # but that will change when the unification of Pre and Post conditions is
    # complete.

    trace_frame_id: DBID = Column(BIGDBIDType, nullable=False, index=True)
    trace_frame = relationship(
        "Precondition",
        primaryjoin=(
            "Precondition.id == " "foreign(TraceFrameAnnotation.trace_frame_id)"
        ),
        uselist=True,
    )

    trace_frame_id2: DBID = Column(BIGDBIDType, nullable=True, index=True)
    trace_frame2 = relationship(
        "TraceFrame",
        primaryjoin=(
            "TraceFrame.id == " "foreign(TraceFrameAnnotation.trace_frame_id2)"
        ),
        uselist=True,
    )


class WarningMessage(Base):  # noqa
    __tablename__ = "warning_messages"

    code = Column(Integer, autoincrement=False, primary_key=True)

    message = Column(String(length=4096), nullable=False)


class IssueTriageInfo(Base):  # noqa
    __tablename__ = "issue_triage_info"

    id = Column("id", BIGINT(unsigned=True), primary_key=True, nullable=False)

    status = Column(
        Enum(
            IssueStatus.UNCATEGORIZED,
            IssueStatus.BAD_PRACTICE,
            IssueStatus.FALSE_POSITIVE,
            IssueStatus.VALID_BUG,
            IssueStatus.DO_NOT_CARE,
            name="issue_states",
        ),
        doc="Shows the issue status from the latest run",
        default=IssueStatus.UNCATEGORIZED,
        nullable=False,
        index=True,
    )

    task_number = Column(
        Integer, doc="Task number (not fbid) that is tracking this " "issue"
    )

    triage_history_fbid = Column(
        BIGINT(unsigned=True),
        nullable=True,
        unique=True,
        doc="FBID for EntZoncolanIssueTriageHistory",
    )

    feedback_fbid = Column(
        BIGINT(unsigned=True),
        nullable=True,
        unique=True,
        doc="FBID for EntZoncolanFeedback",
    )


class IssueTriageInfoAssoc(Base):  # noqa
    __tablename__ = "issue_triage_info_assoc"

    issue_id = Column(BIGINT(unsigned=True), primary_key=True)
    triage_info_id = Column(BIGINT(unsigned=True), primary_key=True)

    issues = relationship(
        Issue,
        primaryjoin="IssueTriageInfoAssoc.issue_id == foreign(Issue.id)",
        uselist=False,
    )

    triage_info = relationship(
        IssueTriageInfo,
        primaryjoin=(
            "IssueTriageInfoAssoc.triage_info_id == " "foreign(IssueTriageInfo.id)"
        ),
        uselist=False,
    )


class WarningCodeCategory(Enum):
    BUG = "bug"
    CODE_SMELL = "code_smell"


class WarningCodeProperties(Base):  # noqa
    """Contains properties describing each warning code"""

    __tablename__ = "warning_code_properties"

    code = Column(
        Integer,
        autoincrement=False,
        nullable=False,
        primary_key=True,
        doc="Code identifiying the issue type",
    )

    category = Column(
        Enum(WarningCodeCategory.BUG, WarningCodeCategory.CODE_SMELL, name="category"),
        nullable=True,
        index=False,
        doc=(
            "The category of problems that issues in with this warning code "
            "can result in ",
        ),
    )

    new_issue_rate = Column(
        Float,
        nullable=True,
        index=False,
        doc="Average number of new issues per day (computed column)",
    )

    bug_count = Column(
        Integer,
        nullable=True,
        index=False,
        doc="Number of issues in this category (computed column)",
    )

    avg_trace_len = Column(
        Float, nullable=True, index=False, doc="Deprecated. See avg_fwd/bwd_trace_len"
    )

    avg_fwd_trace_len = Column(
        Float,
        nullable=True,
        index=False,
        doc=(
            "Average (min) length of forward traces for the given warning code "
            "(computed column)",
        ),
    )

    avg_bwd_trace_len = Column(
        Float,
        nullable=True,
        index=False,
        doc=(
            "Average (min) length of backward traces for the given warning "
            "code (computed column)",
        ),
    )

    snr = Column(
        Float,
        nullable=True,
        index=False,
        doc=(
            "Signal to noise ratio based on triaged issues (computed column). "
            "Ratio of (valid + bad practice) to (false positive + don't care)"
        ),
    )

    is_snr_significant = Column(
        Boolean,
        nullable=True,
        index=False,
        doc=(
            "True if we are confident about the snr (computed column). "
            "Depends on percentage of triaged issues and number of issues."
        ),
    )

    discoverable = Column(
        Boolean,
        nullable=True,
        index=False,
        doc="True if an attacker can discover the issue",
    )

    health_score = Column(
        Float,
        nullable=True,
        index=False,
        doc=(
            "Scoring for the health of the warning code, between 0 and 1, "
            "based on the values in the other columns (computed column)"
        ),
    )

    notes = Column(
        String(length=4096),
        nullable=True,
        index=False,
        doc="Free form field for note-taking",
    )


class PrimaryKey(Base, PrepareMixin, RecordMixin):  # noqa

    __tablename__ = "primary_keys"

    table_name: str = Column(
        String(length=100),
        doc="Name of the table that this row stores the next available primary key for",
        nullable=False,
        primary_key=True,
    )

    current_id: int = Column(
        BIGINT(unsigned=True).with_variant(BIGINT, "sqlite"),
        doc="The current/latest id used in the table.",
        nullable=False,
        primary_key=False,
    )


class PrimaryKeyGenerator:
    """Keep track of DB objects' primary keys by ourselves rather than relying
    on SQLAlchemy, so we can supply them as arguments when creating association
    objects, such as TraceFrameLeafAssoc"""

    QUERY_CLASSES: Set[Type] = {
        Issue,
        IssueInstance,
        IssueInstanceFixInfo,
        SharedText,
        Postcondition,
        Precondition,
        Run,
        Sink,
        Source,
        TraceFrame,
        TraceFrameAnnotation,
    }

    # Map from class name to an ID range (next_id, max_reserved_id)
    pks: Dict[str, Tuple[int, int]] = {}

    def reserve(
        self,
        session: Session,
        saving_classes: List[Type],
        item_counts: Optional[Dict[str, int]] = None,
        use_lock: bool = False,
    ) -> "PrimaryKeyGenerator":
        """
        session - Session for DB operations.
        saving_classes - class objects that need to be saved e.g. Issue, Run
        item_counts - map from class name to the number of items, for preallocating
        id ranges
        """
        query_classes = {cls for cls in saving_classes if cls in self.QUERY_CLASSES}
        for cls in query_classes:
            if item_counts and cls.__name__ in item_counts:
                count = item_counts[cls.__name__]
            else:
                count = 1
            self._reserve_id_range(session, cls, count, use_lock)

        return self

    def _lock_pk_with_retries(
        self, session: Session, cls: Type
    ) -> Optional[PrimaryKey]:
        cls_pk: Optional[PrimaryKey] = None
        retries: int = 3
        while retries > 0:
            try:
                cls_pk = (
                    session.query(PrimaryKey)
                    .filter(PrimaryKey.table_name == cls.__name__)
                    .with_for_update()
                    .first()
                )
                # if we're here, the record has been locked, or there is no record
                retries = 0
            except exc.OperationalError as ex:
                # Failed to get exclusive lock on the record, so we retry
                retries -= 1
                # Re-raise the exception if our retries are exhausted
                if retries == 0:
                    raise ex
        return cls_pk

    def _reserve_id_range(
        self, session: Session, cls: Type, count: int, use_lock: bool
    ) -> None:
        cls_pk = self._lock_pk_with_retries(session, cls)
        if use_lock or not cls_pk:
            # If cls_pk is None, then we query the data table for the max ID
            # and use that as the current_id in the primary_keys table. This
            # should only occur once (the except with a rollback means any
            # additional attempt will fail to add a row, and use the "current"
            # id value)
            row = session.query(cls.id).order_by(cls.id.desc()).first()
            try:
                if not cls_pk:
                    session.execute(
                        "INSERT INTO primary_keys(table_name, current_id) \
                        VALUES (:table_name, :current_id)",
                        {
                            "table_name": cls.__name__,
                            "current_id": (row.id) if row else 0,
                        },
                    )
                else:
                    cls_pk.current_id = row.id if row else 0
                session.commit()
            except exc.SQLAlchemyError:
                session.rollback()
            cls_pk = self._lock_pk_with_retries(session, cls)

        if cls_pk:
            next_id = cls_pk.current_id + 1
            cls_pk.current_id = cls_pk.current_id + count
            pk_entry: Tuple[int, int] = (next_id, cls_pk.current_id)
            session.commit()
            self.pks[cls.__name__] = pk_entry

    def get(self, cls):
        assert cls in self.QUERY_CLASSES, (
            "%s primary key should be generated by SQLAlchemy" % cls.__name__
        )
        assert cls.__name__ in self.pks, (
            "%s primary key needs to be initialized before use" % cls.__name__
        )
        (pk, max_pk) = self.pks[cls.__name__]
        assert pk <= max_pk, "%s reserved primary key range exhausted" % cls.__name__
        self.pks[cls.__name__] = (pk + 1, max_pk)
        return pk


def create(engine):
    Base.metadata.create_all(engine)
