import logging

import django.db
from django import VERSION
from django.apps import apps
from django.db import models, migrations
from django.db.migrations.migration import Migration
from django.db.migrations.state import ProjectState
from django.db.models import UniqueConstraint
from django.db.utils import DEFAULT_DB_ALIAS, ConnectionHandler, ProgrammingError
from django.test import TestCase
from unittest import skipIf

from . import get_constraints
from ..models import (
    TestIndexesRetainedRenamed,
    Choice,
    Question,
)

connections = ConnectionHandler()

if (VERSION >= (3, 2)):
    from django.utils.connection import ConnectionProxy
    connection = ConnectionProxy(connections, DEFAULT_DB_ALIAS)
else:
    from django.db import DefaultConnectionProxy
    connection = DefaultConnectionProxy()

logger = logging.getLogger('mssql.tests')


class TestIndexesRetained(TestCase):
    """
    Issue https://github.com/microsoft/mssql-django/issues/14
    Indexes dropped during a migration should be re-created afterwards
    assuming the field still has `db_index=True`
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Pre-fetch which indexes exist for the relevant test model
        # now that all the test migrations have run
        cls.constraints = get_constraints(table_name=TestIndexesRetainedRenamed._meta.db_table)
        cls.indexes = {k: v for k, v in cls.constraints.items() if v['index'] is True}

    def _assert_index_exists(self, columns):
        matching = {k: v for k, v in self.indexes.items() if set(v['columns']) == columns}
        assert len(matching) == 1, (
            "Expected 1 index for columns %s but found %d %s" % (
                columns,
                len(matching),
                ', '.join(matching.keys())
            )
        )

    def test_field_made_nullable(self):
        # case (a) of https://github.com/microsoft/mssql-django/issues/14
        self._assert_index_exists({'a'})

    def test_field_renamed(self):
        # case (b) of https://github.com/microsoft/mssql-django/issues/14
        self._assert_index_exists({'b_renamed'})

    def test_table_renamed(self):
        # case (c) of https://github.com/microsoft/mssql-django/issues/14
        self._assert_index_exists({'c'})

def _get_all_models():
    for app in apps.get_app_configs():
        app_label = app.label
        for model_name, model_class in app.models.items():
            yield model_class, model_name, app_label


class TestCorrectIndexes(TestCase):

    def test_correct_indexes_exist(self):
        """
        Check there are the correct number of indexes for each field after all migrations
        by comparing what the model says (e.g. `db_index=True` / `index_together` etc.)
        with the actual constraints found in the database.
        This acts as a general regression test for issues such as:
         - duplicate index created (e.g. https://github.com/microsoft/mssql-django/issues/77)
         - index dropped but accidentally not recreated
         - index incorrectly 'recreated' when it was never actually dropped or required at all
        Note of course that it only covers cases which exist in testapp/models.py and associated migrations
        """
        connection = django.db.connections[django.db.DEFAULT_DB_ALIAS]
        for model_cls, model_name, app_label in _get_all_models():
            logger.debug('Checking model: %s.%s', app_label, model_name)
            if not model_cls._meta.managed:
                # Models where the table is not managed by Django migrations are irrelevant
                continue
            model_constraints = get_constraints(table_name=model_cls._meta.db_table)
            # Check correct indexes are in place for all fields in model
            for field in model_cls._meta.get_fields():
                if not hasattr(field, 'column'):
                    # ignore things like reverse fields which don't have a column on this table
                    continue
                col_name = connection.introspection.identifier_converter(field.column)
                field_str = f'{app_label}.{model_name}.{field.name} ({col_name})'
                logger.debug('  > Checking field: %s', field_str)

                # Find constraints which include this column
                col_constraints = [
                    dict(name=name, **infodict) for name, infodict in model_constraints.items()
                    if col_name in infodict['columns']
                ]
                col_indexes = [c for c in col_constraints if c['index']]
                for c in col_constraints:
                    logger.debug('    > Column <%s> is involved in constraint: %s', col_name, c)

                # There should be an explicit index for each of the following cases
                expected_index_causes = []
                if field.db_index:
                    expected_index_causes.append('db_index=True')
                for field_names in model_cls._meta.index_together:
                    if field.name in field_names:
                        expected_index_causes.append(f'index_together[{field_names}]')
                if field._unique and field.null:
                    # This is implemented using a (filtered) unique index (not a constraint) to get ANSI NULL behaviour
                    expected_index_causes.append('unique=True & null=True')
                for field_names in model_cls._meta.unique_together:
                    if field.name in field_names:
                        # unique_together results in an index because this backend implements it using a
                        # (filtered) unique index rather than a constraint, to get ANSI NULL behaviour
                        expected_index_causes.append(f'unique_together[{field_names}]')
                for uniq_constraint in filter(lambda c: isinstance(c, UniqueConstraint), model_cls._meta.constraints):
                    if field.name in uniq_constraint.fields and uniq_constraint.condition is not None:
                        # Meta:constraints > UniqueConstraint with condition are implemented with filtered unique index
                        expected_index_causes.append(f'UniqueConstraint (with condition) in Meta: constraints')

                # Other cases like `unique=True, null=False` or `field.primary_key` do have index-like constraints
                # but in those cases the introspection returns `"index": False` so they are not in the list of
                # explicit indexes which we are checking here (`col_indexes`)

                assert len(col_indexes) == len(expected_index_causes), \
                    'Expected %s index(es) on %s but found %s.\n' \
                    'Check for behaviour changes around index drop/recreate in methods like _alter_field.\n' \
                    'Expected due to: %s\n' \
                    'Found: %s' % (
                        len(expected_index_causes),
                        field_str,
                        len(col_indexes),
                        expected_index_causes,
                        '\n'.join(str(i) for i in col_indexes),
                    )
                logger.debug('  Found %s index(es) as expected', len(col_indexes))


class TestIndexesBeingDropped(TestCase):

    def test_unique_index_dropped(self):
        """
        Issues https://github.com/microsoft/mssql-django/issues/110
        and https://github.com/microsoft/mssql-django/issues/90
        Unique indexes not being dropped when changing non-nullable
        foreign key with unique_together to nullable causing
        dependent on column error
        """
        old_field = Choice._meta.get_field('question')
        new_field = models.ForeignKey(
            Question, null=False, on_delete=models.deletion.CASCADE
        )
        new_field.set_attributes_from_name("question")
        with connection.schema_editor() as editor:
            editor.alter_field(Choice, old_field, new_field, strict=True)

        old_field = new_field
        new_field = models.ForeignKey(
            Question, null=True, on_delete=models.deletion.CASCADE
        )
        new_field.set_attributes_from_name("question")
        try:
            with connection.schema_editor() as editor:
                editor.alter_field(Choice, old_field, new_field, strict=True)
        except ProgrammingError:
            self.fail("Unique indexes not being dropped")

class TestAddAndAlterUniqueIndex(TestCase):

    def test_alter_unique_nullable_to_non_nullable(self):
        """
        Test a single migration that creates a field with unique=True and null=True and then alters
        the field to set null=False. See https://github.com/microsoft/mssql-django/issues/22
        """
        operations = [
            migrations.CreateModel(
                "TestAlterNullableInUniqueField",
                [
                    ("id", models.AutoField(primary_key=True)),
                    ("a", models.CharField(max_length=4, unique=True, null=True)),
                ]
            ),
            migrations.AlterField(
                "testalternullableinuniquefield",
                "a",
                models.CharField(max_length=4, unique=True)
            )
        ]

        project_state = ProjectState()
        new_state = project_state.clone()
        migration = Migration("name", "testapp")
        migration.operations = operations

        try:
            with connection.schema_editor(atomic=True) as editor:
                migration.apply(new_state, editor)
        except django.db.utils.ProgrammingError as e:
            self.fail('Check if can alter field from unique, nullable to unique non-nullable for issue #23, AlterField failed with exception: %s' % e)

class TestKeepIndexWithDbcomment(TestCase):
    def _find_key_with_type_idx(self, input_dict):
        for key, value in input_dict.items():
            if value.get("type") == "idx":
                return key
        return None

    @skipIf(VERSION < (4, 2), "db_comment not available before 4.2")
    def test_drop_foreignkey(self):
        app_label = "test_drop_foreignkey"
        operations = [
                migrations.CreateModel(
                    name="brand",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        ("name", models.CharField(max_length=100)),
                    ],
                ),
                migrations.CreateModel(
                    name="car1",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="test_drop_foreignkey.brand",
                                related_name="car1",
                                db_constraint=True,
                            ),
                        ),
                    ],
                ),
                migrations.CreateModel(
                    name="car2",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="test_drop_foreignkey.brand",
                                related_name="car2",
                                db_constraint=True,
                            ),
                        ),
                    ],
                ),
                migrations.CreateModel(
                    name="car3",
                    fields=[
                        ("id", models.AutoField(primary_key=True)),
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                to="test_drop_foreignkey.brand",
                                related_name="car3",
                                db_constraint=True,
                            ),
                        ),
                    ],
                ),
            ]
        migration = Migration("name", app_label)
        migration.operations = operations
        with connection.schema_editor(atomic=True) as editor:
            project_state = migration.apply(ProjectState(), editor)

        alter_fk_car1 = migrations.AlterField(
            model_name="car1",
            name="brand",
            field=models.ForeignKey(
                to="test_drop_foreignkey.brand",
                on_delete=django.db.models.deletion.CASCADE,
                db_constraint=False,
                related_name="car1",
            ),
        )
        alter_fk_car2 = migrations.AlterField(
            model_name="car2",
            name="brand",
            field=models.ForeignKey(
                to="test_drop_foreignkey.brand",
                on_delete=django.db.models.deletion.CASCADE,
                db_constraint=False,
                related_name="car2",
                db_comment=""
            ),
        )
        alter_fk_car3 = migrations.AlterField(
            model_name="car3",
            name="brand",
            field=models.ForeignKey(
                to="test_drop_foreignkey.brand",
                on_delete=django.db.models.deletion.CASCADE,
                db_constraint=False,
                related_name="car3",
                db_comment="fk_on_delete_keep_index"
            ),
        )
        new_state = project_state.clone()
        with connection.schema_editor(atomic=True) as editor:
            alter_fk_car1.state_forwards("test_drop_foreignkey", new_state)
            alter_fk_car1.database_forwards(
                "test_drop_foreignkey", editor, project_state, new_state
            )
        car_index = self._find_key_with_type_idx(
            get_constraints(
                table_name=new_state.apps.get_model(
                    "test_drop_foreignkey", "car1"
                )._meta.db_table
            )
        )
        # Test alter foreignkey without db_comment field
        # The index should be dropped (keep the old behavior)
        self.assertIsNone(car_index)

        project_state = new_state
        new_state = new_state.clone()
        with connection.schema_editor(atomic=True) as editor:
            alter_fk_car2.state_forwards("test_drop_foreignkey", new_state)
            alter_fk_car2.database_forwards(
                "test_drop_foreignkey", editor, project_state, new_state
            )
        car_index = self._find_key_with_type_idx(
            get_constraints(
                table_name=new_state.apps.get_model(
                    "test_drop_foreignkey", "car2"
                )._meta.db_table
            )
        )
        # Test alter fk with empty db_comment
        self.assertIsNone(car_index)

        project_state = new_state
        new_state = new_state.clone()
        with connection.schema_editor(atomic=True) as editor:
            alter_fk_car3.state_forwards("test_drop_foreignkey", new_state)
            alter_fk_car3.database_forwards(
                "test_drop_foreignkey", editor, project_state, new_state
            )
        car_index = self._find_key_with_type_idx(
            get_constraints(
                table_name=new_state.apps.get_model(
                    "test_drop_foreignkey", "car3"
                )._meta.db_table
            )
        )
        # Test alter fk with fk_on_delete_keep_index in db_comment
        # Index should be preserved in this case
        self.assertIsNotNone(car_index)
