"""Adapter regressions for real self-reference evidence."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from elt_taskgen.adapters import schemapile, wikidbs
from tests.test_adapters_schemapile import _col, _table, _fk
from tests.scenario_fixtures import scenario_source
from tests.scenario_oracle import assert_oracle
from elt_taskgen.generation.scenario_evidence import HIERARCHY
from elt_taskgen.generation.scenario_recipes import apply_scenario_difficulty, bound_scenario
from elt_taskgen.generation.source_data import generate_rows
from elt_taskgen.generation.populations import _synthetic_required_parent_problems
from elt_taskgen.models import Relationship


class ScenarioSourceTests(unittest.TestCase):
    def test_required_self_references_generate_after_own_keys(self):
        for composite in (False, True):
            source=scenario_source(composite=composite, self_graph=True)
            source=source.model_copy(update={'relationships':tuple(r.model_copy(update={'required':True})
                if r.child_table==r.parent_table else r for r in source.relationships),
                'tables':tuple(t.model_copy(update={'columns':tuple(c.model_copy(update={'nullable':False})
                    for c in t.columns)}) if t.name=='groups' else t for t in source.tables)})
            task=apply_scenario_difficulty(source,recipe=HIERARCHY)
            mart=task.marts[-1]
            e=bound_scenario(task,mart)
            self.assertFalse(_synthetic_required_parent_problems(task))
            for population in task.populations:
                with self.subTest(composite=composite,population=population.name):
                    rows=generate_rows(task,population.name)
                    self.assertEqual(rows,generate_rows(task,population.name))
                    keys={tuple(row[c] for c in e.keys) for row in rows[e.parent]}
                    self.assertTrue(all(tuple(row[c] for c in e.links) in keys for row in rows[e.fact]))
                    assert_oracle(task,mart,e,rows)

    def test_self_links_preserve_shared_identity_components(self):
        source=scenario_source(composite=True,self_graph=True)
        realms=source.table('groups').model_copy(update={'name':'realms',
            'columns':(source.table('groups').column('realm'),),'primary_key':('realm',)})
        source=source.model_copy(update={'relationships':tuple(r.model_copy(update={
            'required':True,'child_columns':('realm','parent_id')})
            if r.child_table==r.parent_table else r for r in source.relationships)+(Relationship(
                child_table='groups',child_columns=('realm',),parent_table='realms',parent_columns=('realm',),required=True),),
            'tables':(*source.tables,realms),
            'backends':(*source.backends,source.backends[0].model_copy(update={'table':'realms'})),
            'populations':tuple(p.model_copy(update={'scale':{**p.scale,'realms':3}}) for p in source.populations)})
        for population in source.populations:
            generated=generate_rows(source,population.name)
            rows=generated['groups']
            keys={(r['realm'],r['id']) for r in rows}
            self.assertTrue(all((r['realm'],r['parent_id']) in keys for r in rows))
            self.assertTrue({r['realm'] for r in rows}<={r['realm'] for r in generated['realms']})
            self.assertEqual(len(keys),len(rows))

    def test_schemapile_preserves_valid_composite_self_references(self):
        raw = {'groups': _table({
            'tenant': _col('Int', nullable=False), 'id': _col('Int', nullable=False),
            'parent_tenant': _col('Int'), 'parent_id': _col('Int'),
        }, pks=('tenant', 'id'), fks=(
            _fk(('parent_id', 'parent_tenant'), 'GROUPS', ('ID', 'TENANT')),
            _fk(('missing',), 'groups', ('id',)),
        ))}
        tables, rels = schemapile.record_to_tables({'TABLES': raw})
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0].child_table, rels[0].parent_table)
        self.assertEqual(rels[0].child_columns, ('parent_id', 'parent_tenant'))
        self.assertEqual(rels[0].parent_columns, ('id', 'tenant'))
        self.assertEqual(tables[0].primary_key, ('id', 'tenant'))
        self.assertFalse(rels[0].required)
        without_edge = {'groups': {**raw['groups'], 'FOREIGN_KEYS': []}}
        self.assertNotEqual(schemapile.shape_fingerprint(raw), schemapile.shape_fingerprint(without_edge))

    def test_wikidbs_keeps_only_row_supported_self_edges_without_changing_rows(self):
        schema = {'database_name': 'groups', 'tables': [{
            'table_name': 'groups', 'file_name': 'groups.csv',
            'columns': [{'column_name': c, 'data_type': 'string'} for c in ('name', 'parent_name')],
            'foreign_keys': [{'column_name': 'parent_name', 'reference_column_name': 'name',
                              'reference_table_name': 'groups'}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'tables').mkdir()
            (root / 'schema.json').write_text(json.dumps(schema))
            for parents, supported in ((['B', 'A', '', 'missing'], True), (['x', 'y', '', 'z'], False)):
                with (root / 'tables/groups.csv').open('w', newline='') as stream:
                    csv.writer(stream).writerows([['name', 'parent_name'], *zip('ABCD', parents)])
                result = wikidbs._convert(root, wikidbs.load_schema(root))
                self.assertEqual(len(result.relationships), int(supported))
                self.assertEqual([r['name'] for r in result.rows['groups']], list('ABCD'))
                self.assertEqual([r['parent_name'] for r in result.rows['groups']], [p or None for p in parents])
                if supported:
                    edge = result.relationships[0]
                    self.assertEqual(edge.child_table, edge.parent_table)
                    self.assertFalse(edge.required)
                    self.assertEqual(result.tables[0].business_key, ('name',))


if __name__ == '__main__':
    unittest.main()
