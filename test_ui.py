"""Streamlit interaction tests using an isolated temporary database, never live APIs."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import db
from ai_utils import PROMPT_VERSION, model_name
from product_profile import PROFILE_HASH
from test_core import record, ai_result


class DashboardTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        (root / 'runtime').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root / 'runtime')
        self.engine = db.init_db(db.engine_for('sqlite:///' + self.directory.name + '/test.db'))
        _, version, _ = db.save_notice(record(), self.engine)
        db.save_analysis(version, ai_result(), PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)
        self.engine_patch = patch.object(db, '_engine', self.engine)
        self.env_patch = patch.dict(os.environ, {'ADMIN_PASSWORD': ''})
        self.engine_patch.start()
        self.env_patch.start()
        self.app = AppTest.from_file(str(root / 'app.py'), default_timeout=30).run()

    def tearDown(self):
        self.env_patch.stop()
        self.engine_patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def element(self, elements, label):
        return next(element for element in elements if element.label == label)

    def test_dashboard_shows_persisted_analysis(self):
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.title[0].value, '공공 IT Insight')
        self.assertEqual(self.element(self.app.metric, 'AI 분석 완료').value, '1')

    def test_literal_bracket_search_does_not_crash(self):
        self.element(self.app.text_input, '제목·기관·태그 검색').set_value('[')
        self.element(self.app.button, '검색').click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(any('조건에 맞는' in info.value for info in self.app.info))

    def test_product_page_marks_mbust_as_future_only(self):
        self.app.radio[0].set_value('제품 기준').run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(any('예정 명칭' in info.value for info in self.app.info))

    def test_no_admin_secret_means_no_public_paid_execution(self):
        self.assertTrue(self.element(self.app.button, '최신 정보 수집 및 분석').disabled)

    def test_rnd_view_does_not_reclassify_business_notice(self):
        self.app.radio[0].set_value('R&D').run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(any('조건에 맞는' in info.value for info in self.app.info))


if __name__ == '__main__':
    unittest.main()
