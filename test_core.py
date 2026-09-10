"""Offline regression tests: no real API calls and no production database writes."""
import copy
import json
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, text
import db
import main
from ai_utils import PROMPT_VERSION, model_name, safe_ai_error, validate_analysis
from collectors import parse_g2b_xml, parse_nia_detail
from product_profile import PRODUCTS, PROFILE_HASH


BODY = ('공공 예약 서비스의 동시 접속을 관리하기 위한 가상 대기실 시스템 구축 사업입니다. '
        '온라인 신청 접수 시 서버 과부하를 방지하기 위해 순번 대기 기능을 도입합니다. 연구개발비 지원 사업은 아닙니다.')


def record(**kwargs):
    result = {'agency': '테스트기관', 'organization': '테스트기관', 'title': '가상 대기실 구축 입찰',
              'content': BODY, 'url': 'https://example.org/list', 'reg_date': '2026-09-10', 'due_date': '2026-09-20'}
    result.update(kwargs)
    return result


def ai_result():
    return {
        'category': '입찰', 'subtype': '시스템 구축', 'tags': ['IT'], 'track': 'BIZ',
        'rnd': False, 'support': False, 'ai_related': None, 'it_related': True,
        'system_build': True, 'cloud_related': None, 'security_related': None,
        'relevance_score': 90, 'importance_score': None, 'confidence': 80,
        'summary': '공공 예약 서비스의 대기실 구축 사업입니다.', 'key_points': ['순번 대기'],
        'evidence': [{'id': 'e1', 'field': 'original_content', 'quote': '가상 대기실 시스템 구축 사업입니다.'}],
        'reasons': [{'claim': '시스템 구축 입찰로 분석합니다.', 'kind': 'AI 분석', 'evidence_ids': ['e1']}],
        'product_matches': [{'product': 'NetFUNNEL', 'score': 90, 'reason': '대기실 요구와 관련됩니다.',
            'evidence_ids': ['e1'], 'product_source': PRODUCTS[0]['sources'][0], 'kind': 'AI 분석'}],
        'uncertainties': ['첨부 본문 미확보'],
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = db.init_db(db.engine_for('sqlite:///:memory:'))

    def tearDown(self):
        self.engine.dispose()

    def count(self, table):
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar_one()

    def test_same_notice_does_not_duplicate(self):
        self.assertEqual(db.save_notice(record(), self.engine)[2], 'new')
        self.assertEqual(db.save_notice(record(), self.engine)[2], 'unchanged')
        self.assertEqual(self.count(db.originals), 1)

    def test_shared_list_url_does_not_overwrite(self):
        db.save_notice(record(title='공고 A'), self.engine)
        db.save_notice(record(title='공고 B'), self.engine)
        self.assertEqual(self.count(db.notices), 2)

    def test_different_agencies_remain_separate(self):
        db.save_notice(record(), self.engine)
        db.save_notice(record(agency='다른기관'), self.engine)
        self.assertEqual(self.count(db.notices), 2)

    def test_stable_number_preserves_changed_title_as_version(self):
        db.save_notice(record(notice_number='123'), self.engine)
        db.save_notice(record(notice_number='123', title='정정 공고'), self.engine)
        self.assertEqual(self.count(db.notices), 1)
        self.assertEqual(self.count(db.originals), 2)

    def test_changed_deadline_keeps_original_snapshot(self):
        db.save_notice(record(notice_number='123'), self.engine)
        db.save_notice(record(notice_number='123', due_date='2026-10-01'), self.engine)
        with self.engine.connect() as conn:
            values = [json.loads(r[0])['deadline'] for r in conn.execute(select(db.originals.c.payload))]
        self.assertEqual(set(values), {'2026-09-20', '2026-10-01'})

    def test_view_counter_does_not_trigger_reanalysis(self):
        db.save_notice(record(views='1', raw_html='<b>1</b>'), self.engine)
        self.assertEqual(db.save_notice(record(views='2', raw_html='<b>2</b>'), self.engine)[2], 'unchanged')

    def test_title_only_is_not_a_body(self):
        r = record()
        r['content'] = r['title']
        self.assertEqual(db.normalize_record(r)['content_quality'], 'metadata_only')

    def test_invalid_dates_are_unknown_not_fabricated(self):
        for value in ['4,738', '마감', '2026-02-31', '', None]:
            self.assertIsNone(db.valid_date(value))
        self.assertEqual(db.valid_date('2026.9.10'), '2026-09-10')

    def test_tracking_query_removed_but_identity_retained(self):
        self.assertEqual(db.canonical_url('https://example.org/view?id=12&utm_source=x'), 'https://example.org/view?id=12')
        self.assertEqual(db.canonical_url('javascript:alert(1)'), '')

    def test_empty_title_rejected(self):
        with self.assertRaises(ValueError):
            db.save_notice(record(title=''), self.engine)

    def test_analysis_visible_only_for_current_product_profile(self):
        _, version, _ = db.save_notice(record(), self.engine)
        db.save_analysis(version, ai_result(), PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)
        self.assertIsNotNone(db.list_notices(PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)[0]['analysis'])
        self.assertIsNone(db.list_notices('new-product-version', PROMPT_VERSION, model_name(), self.engine)[0]['analysis'])

    def test_changed_original_invalidates_displayed_analysis(self):
        _, version, _ = db.save_notice(record(notice_number='1'), self.engine)
        db.save_analysis(version, ai_result(), PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)
        db.save_notice(record(notice_number='1', content=BODY+' 수정 내용입니다.'), self.engine)
        self.assertIsNone(db.list_notices(PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)[0]['analysis'])
        self.assertEqual(self.count(db.analyses), 1)

    def test_duplicate_analysis_is_not_reinserted(self):
        _, version, _ = db.save_notice(record(), self.engine)
        args = (version, ai_result(), PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)
        self.assertTrue(db.save_analysis(*args))
        self.assertFalse(db.save_analysis(*args))

    def test_lock_excludes_second_worker(self):
        self.assertTrue(db.acquire_lock('one', engine=self.engine))
        self.assertFalse(db.acquire_lock('two', engine=self.engine))
        db.release_lock('one', self.engine)
        self.assertTrue(db.acquire_lock('two', engine=self.engine))

    def test_daily_limit_counts_failed_attempts_too(self):
        self.assertTrue(db.reserve_ai_attempt('id1', 1, self.engine))
        self.assertFalse(db.reserve_ai_attempt('id2', 1, self.engine))

    def test_legacy_import_drops_unverified_product_and_ai_claims(self):
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE postings (title TEXT, agency TEXT, recommended_solution TEXT, ai_summary TEXT)'))
            conn.execute(text("INSERT INTO postings VALUES ('기존 공고', '기관', 'MBUSTER', '구 제품 주장')"))
        self.assertEqual(db.import_legacy(self.engine), 1)
        self.assertEqual(db.import_legacy(self.engine), 0)
        row = db.list_notices(PROFILE_HASH, PROMPT_VERSION, model_name(), self.engine)[0]
        self.assertNotIn('구 제품 주장', json.dumps(row, ensure_ascii=False))
        self.assertIsNone(row['analysis'])
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM postings')).scalar_one(), 1)

    def test_pipeline_success_and_second_run_no_ai(self):
        with patch.dict(main.COLLECTORS, {'fixture': lambda limit: [record()]}), \
             patch.object(main, 'is_gemini_ready', return_value=True), \
             patch.object(main, 'analyze_notice', return_value=ai_result()) as ai:
            result = main.run_pipeline(['fixture'], engine=self.engine)
            again = main.run_pipeline(['fixture'], engine=self.engine)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(again['unchanged'], 1)
        self.assertEqual(ai.call_count, 1)

    def test_failure_does_not_look_like_zero_success(self):
        def fail(limit):
            raise RuntimeError('https://example.org?serviceKey=SECRET')
        with patch.dict(main.COLLECTORS, {'fixture': fail}):
            result = main.run_pipeline(['fixture'], engine=self.engine, analyze=False)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_ai_failure_preserves_original_and_can_retry(self):
        with patch.dict(main.COLLECTORS, {'fixture': lambda limit: [record()]}), \
             patch.object(main, 'is_gemini_ready', return_value=True), \
             patch.object(main, 'analyze_notice', side_effect=ValueError('invalid evidence')):
            result = main.run_pipeline(['fixture'], engine=self.engine)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.count(db.originals), 1)
        self.assertEqual(self.count(db.analyses), 0)


class EvidenceTests(unittest.TestCase):
    def test_valid_evidence_accepted(self):
        self.assertEqual(validate_analysis(ai_result(), db.normalize_record(record()))['track'], 'BIZ')

    def test_invented_quote_rejected(self):
        result = ai_result()
        result['evidence'][0]['quote'] = '원문에 없는 예산 100억원'
        with self.assertRaises(ValueError):
            validate_analysis(result, db.normalize_record(record()))

    def test_missing_evidence_reference_rejected(self):
        result = ai_result()
        result['reasons'][0]['evidence_ids'] = ['missing']
        with self.assertRaises(ValueError):
            validate_analysis(result, db.normalize_record(record()))

    def test_future_product_cannot_be_recommended_as_available(self):
        result = ai_result()
        result['product_matches'][0]['product'] = 'MBUSTER'
        with self.assertRaises(ValueError):
            validate_analysis(result, db.normalize_record(record()))

    def test_unapproved_product_source_rejected(self):
        result = ai_result()
        result['product_matches'][0]['product_source'] = '제품소개서_MBUSTER.pdf'
        with self.assertRaises(ValueError):
            validate_analysis(result, db.normalize_record(record()))

    def test_unknown_boolean_not_forced_to_false(self):
        self.assertIsNone(validate_analysis(ai_result(), db.normalize_record(record()))['ai_related'])

    def test_scores_outside_range_rejected(self):
        result = ai_result()
        result['relevance_score'] = 101
        with self.assertRaises(ValueError):
            validate_analysis(result, db.normalize_record(record()))

    def test_raw_exception_never_shown(self):
        self.assertNotIn('SECRET', safe_ai_error(RuntimeError('SECRET')))


class CollectorTests(unittest.TestCase):
    def test_nia_body_not_entire_navigation(self):
        html = '''<nav>사이트 메뉴</nav><div id="sub_contentsArea2" class="detail_type01">
        <h3>입찰 안내</h3><span>2026.09.10</span><div class="con_area">공고 본문</div>
        <a href="/common/board/Download.do?bcIdx=123">제안서.hwp</a></div>'''
        row = parse_nia_detail(html, 'https://www.nia.or.kr/view', '123')
        self.assertEqual(row['content'], '공고 본문')
        self.assertEqual(row['title'], '입찰 안내')
        self.assertEqual(row['reg_date'], '2026-09-10')
        self.assertEqual(len(row['attachments']), 1)

    def test_nia_structure_failure_is_explicit(self):
        with self.assertRaises(ValueError):
            parse_nia_detail('<html>access denied</html>', 'https://www.nia.or.kr/', '1')

    def test_g2b_notice_number_and_original_response_preserved(self):
        xml = '<response><header><resultCode>00</resultCode></header><body><items><item><bidNtceNo>N1</bidNtceNo><bidNtceOrd>01</bidNtceOrd><bidNtceNm>입찰</bidNtceNm></item></items></body></response>'
        row = parse_g2b_xml(xml)[0]
        self.assertEqual(row['notice_number'], 'N1')
        self.assertEqual(row['revision'], '01')
        self.assertEqual(row['raw_payload']['bidNtceNm'], '입찰')
        self.assertEqual(row['content'], '')

    def test_g2b_auth_error_not_empty_success(self):
        with self.assertRaises(RuntimeError):
            parse_g2b_xml('<response><header><resultCode>30</resultCode></header></response>')


if __name__ == '__main__':
    unittest.main()
