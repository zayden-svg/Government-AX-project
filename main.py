"""Canonical collection and analysis entry point. Does not start on import."""
import argparse
import json
import uuid

from ai_utils import PROMPT_VERSION, analyze_notice, is_gemini_ready, model_name, safe_ai_error
from collectors import COLLECTORS
from db import (acquire_lock, finish_job, import_legacy, init_db, list_notices,
                release_lock, reserve_ai_attempt, save_analysis, save_notice, start_job)
from product_profile import PROFILE_HASH
from settings import bounded_int, setting

# Existing experimental collectors are retained but not enabled by default.
VERIFIED_SOURCES = ['NIA', '조달청']


def enabled_sources():
    return [s.strip() for s in setting('ENABLED_COLLECTORS', 'NIA,조달청').split(',') if s.strip()]


def run_pipeline(sources=None, collect=True, analyze=True, limit=None, trigger='manual', engine=None):
    engine = init_db(engine)
    sources = enabled_sources() if sources is None else sources
    if collect and (not sources or any(s not in COLLECTORS for s in sources)):
        raise ValueError('수집 대상 설정을 확인해 주세요.')
    limit = min(max(int(limit or bounded_int('COLLECTION_LIMIT', 10, 1, 25)), 1), 25)
    owner = uuid.uuid4().hex
    if not acquire_lock(owner, engine=engine):
        return {'status': 'busy', 'message': '다른 수집·분석 작업이 진행 중입니다.'}
    job_id = None
    report = {'sources': {}, 'new': 0, 'updated': 0, 'unchanged': 0,
              'analyzed': 0, 'analysis_failed': 0, 'insufficient': 0, 'analysis_pending': 0,
              'errors': [], 'scope': '등록된 기관의 제한된 최신 목록만 수집; 전국 전체 수집 아님'}
    try:
        job_id = start_job(trigger, engine)
        if collect:
            for source in sources:
                try:
                    records = COLLECTORS[source](limit=limit)
                    saved = 0
                    rejected = 0
                    for record in records:
                        try:
                            _, _, state = save_notice(record, engine)
                            report[state] += 1
                            saved += 1
                        except (ValueError, TypeError):
                            rejected += 1
                    report['sources'][source] = {'status': 'partial' if rejected else 'success',
                        'received': len(records), 'saved': saved, 'rejected': rejected,
                        'coverage': f'최대 {limit}건, 첫 목록 위주'}
                    if rejected:
                        report['errors'].append(f'{source}: 필수정보 검증에서 {rejected}건 제외')
                except Exception:
                    report['sources'][source] = {'status': 'failed', 'message': '접속·인증·응답 구조 확인 필요'}
                    report['errors'].append(f'{source}: 수집 실패 (원문·키가 포함될 수 있는 상세 오류는 비공개)')
        if analyze:
            records = list_notices(PROFILE_HASH, PROMPT_VERSION, model_name(), engine)
            pending = [r for r in records if not r['analysis']]
            report['insufficient'] = sum(r['original']['content_quality'] != 'body' for r in pending)
            candidates = [r for r in pending if r['original']['content_quality'] == 'body']
            if candidates and not is_gemini_ready():
                report['errors'].append('Gemini 키 미설정 — 수집 원문은 보존됨')
            elif candidates:
                batch = bounded_int('MAX_ANALYSES_PER_RUN', 5, 1, 25)
                daily = bounded_int('MAX_ANALYSES_PER_DAY', 50, 1, 1000)
                for row in candidates[:batch]:
                    if not reserve_ai_attempt(row['original_id'], daily, engine):
                        report['errors'].append('일일 AI 호출 상한 도달 — 다음 날 재시도')
                        break
                    try:
                        result = analyze_notice(row['original'])
                        save_analysis(row['original_id'], result, PROFILE_HASH, PROMPT_VERSION, model_name(), engine)
                        report['analyzed'] += 1
                    except Exception as error:
                        report['analysis_failed'] += 1
                        report['errors'].append(safe_ai_error(error))
                        if getattr(error, 'code', None) in (401, 403, 429):
                            # Authentication and quota problems affect the whole batch.
                            break
                report['analysis_pending'] = len(candidates) - report['analyzed']
        status = 'partial' if report['errors'] else 'success'
        if collect and report['sources'] and all(v['status'] == 'failed' for v in report['sources'].values()):
            status = 'failed'
        report['status'] = status
        finish_job(job_id, report, status, engine)
        return report
    except Exception:
        report['status'] = 'failed'
        report['errors'].append('저장 또는 처리 중 오류 — 기존 원문은 삭제하지 않음')
        if job_id:
            finish_job(job_id, report, 'failed', engine)
        return report
    finally:
        release_lock(owner, engine)


def cli():
    parser = argparse.ArgumentParser(description='공공 IT Insight 수집·Gemini 분석')
    parser.add_argument('--sources', help='예: NIA,조달청')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--no-ai', action='store_true')
    parser.add_argument('--analyze-only', action='store_true')
    parser.add_argument('--import-legacy', action='store_true')
    parser.add_argument('--trigger', default='cli')
    args = parser.parse_args()
    engine = init_db()
    if args.import_legacy:
        owner = uuid.uuid4().hex
        if not acquire_lock(owner, engine=engine):
            raise SystemExit('다른 작업이 진행 중입니다.')
        try:
            print(json.dumps({'legacy_imported': import_legacy(engine)}, ensure_ascii=False))
        finally:
            release_lock(owner, engine)
        return
    sources = args.sources.split(',') if args.sources else None
    result = run_pipeline(sources=sources, collect=not args.analyze_only,
                          analyze=not args.no_ai, limit=args.limit, trigger=args.trigger, engine=engine)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result['status'] in ('failed', 'partial'):
        raise SystemExit(1)


if __name__ == '__main__':
    cli()
