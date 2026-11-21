import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.depgraph import part_graph
from core.ingest import SCHEMA


class PartGraphTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "soony.db"
        self.enterContext(patch.dict(os.environ, {"SOONY_NOW": "2026-11-21T12:00:00+01:00"}))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA)
            conn.executescript("""
                INSERT INTO products(product_id, name) VALUES ('FG-GH6-CE', 'Collector''s Edition');
                INSERT INTO launch_status VALUES ('FG-GH6-CE', '2026-11-21', 100, 0, 100, 10, 'launch plan');
                INSERT INTO components(component_id, description) VALUES
                    ('CMP-GAM-007', 'Collector''s Box Grand Heist 6'),
                    ('CMP-MAG-005', 'NdFeB Disc Magnet 4x1 (lid closure)'),
                    ('CMP-GAM-003', 'Clear case');
                INSERT INTO bom(parent_id, child_id, qty_per) VALUES
                    ('FG-GH6-CE', 'CMP-GAM-007', 1),
                    ('FG-GH6-CE', 'CMP-MAG-005', 4);
                INSERT INTO suppliers(supplier_id, name, status) VALUES ('SUP-0029', 'Guangzhou PackRight', 'at risk');
                INSERT INTO supplier_components(supplier_id, component_id, share, is_primary, note) VALUES
                    ('SUP-0029', 'CMP-GAM-007', 1, 'Y', ''),
                    ('SUP-0029', 'CMP-MAG-005', 1, 'Y', 'Fitted by supplier into CMP-GAM-007; stocked with the box'),
                    ('SUP-0029', 'CMP-GAM-003', 1, 'Y', '');
                INSERT INTO shipments(shipment_id, mode, supplier_id, material_ids, eta, status) VALUES
                    ('ship-box', 'sea', 'SUP-0029', 'CMP-GAM-003|CMP-GAM-007', '2026-11-28', 'in_transit'),
                    ('ship-case', 'sea', 'SUP-0029', 'CMP-GAM-003', '2026-11-28', 'in_transit'),
                    ('ship-delivered', 'sea', 'SUP-0029', 'CMP-GAM-007', '2026-11-20', 'delivered');
                INSERT INTO documents(path, supplier_id, doc_type, text) VALUES (
                    'export-notice.pdf', 'SUP-0029', 'notices',
                    'Our magnet supplier Jiangxi RareMag Materials Co., Ltd. (Ganzhou) cannot export its magnets.'
                );
            """)
            fact = {"material_id": "CMP-MAG-005", "reason": "Rare-earth export licence required.",
                    "unblock_condition": "Licence granted."}
            conn.execute("INSERT INTO doc_facts VALUES (?, 'supplier_hold', ?, 'test')",
                         ("export-notice.pdf", json.dumps(fact)))
            conn.commit()

    def test_box_has_fitted_magnet_supplier_and_document_risk(self):
        graph = part_graph("CMP-GAM-007", self.db)
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = {(edge["source"], edge["target"], edge["label"]) for edge in graph["edges"]}
        self.assertEqual(nodes["CMP-MAG-005"]["kind"], "subpart")
        self.assertNotIn("FG-GH6-CE", nodes)  # finished product is not a node; the part says what it is for
        self.assertEqual(nodes["CMP-GAM-007"]["meta"], "For Collector's Edition, 1 each")
        self.assertTrue(graph["short"])  # nothing in stock, so supplier problems count
        self.assertEqual(nodes["SUP-0029"]["status"], "at_risk")
        self.assertEqual(nodes["SUP-0029"]["meta"], "Flagged at risk")
        self.assertEqual(nodes["SUP-0029"]["label"], "Guangzhou PackRight")
        self.assertIn(("CMP-MAG-005", "CMP-GAM-007", "fitted into"), edges)
        self.assertIn(("CMP-MAG-005", "SUP-0029", "made by"), edges)
        risk, = (node for node in nodes.values() if node["kind"] == "risk")
        self.assertEqual(risk["label"], "Jiangxi RareMag Materials")
        self.assertEqual(risk["subtitle"], "Export licence")
        self.assertEqual(risk["status"], "blocked")
        self.assertEqual(risk["meta"], "Export licence")
        self.assertIn(("SUP-0029", risk["id"], "supply risk"), edges)
        self.assertIn(("SUP-0029", "later:SUP-0029", "after launch"), edges)  # after-launch deliveries collapse per supplier
        self.assertNotIn("ship-case", nodes)
        self.assertNotIn("ship-delivered", nodes)

    def test_fitted_part_traces_through_parent(self):
        graph = part_graph("CMP-MAG-005", self.db)
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["CMP-GAM-007"]["kind"], "assembly")
        self.assertIn("later:SUP-0029", nodes)

    def test_part_specific_risk_is_not_applied_to_another_part_from_same_supplier(self):
        graph = part_graph("CMP-GAM-003", self.db)
        self.assertFalse(any(node["kind"] == "risk" for node in graph["nodes"]))

    def test_unknown_part(self):
        self.assertIsNone(part_graph("unknown", self.db))
