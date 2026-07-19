"""Liain 두뇌 — 5계층 기억 시스템.

Core (핵심)       — 항상 context에. 주 사용자 기본정보 + 현재 감정 + 관계 상태.
Semantic (의미)    — 사실 지식. 사람/장소/사물에 대한 정보.
Episodic (에피소드) — 경험/사건. 날짜별 기억. "그때 그 일"
Procedural (절차)  — 행동 패턴. "이럴 땐 이렇게"
Emotional (감정)   — 감정 추적. 주 사용자 기분 변화 + 트리거.

사용법:
  from brain import Brain
  brain = Brain()

  # 시스템 프롬프트용 (항상 주입)
  context = brain.get_context()

  # 대화 후 기억 업데이트
  brain.process_conversation(messages, person=_PRIMARY)

  # 특정 사실 업데이트
  brain.update_semantic("user", "거주지", "서울 마포구")

  # 감정 업데이트
  brain.update_emotion("user", "피곤", "야근 때문")

  # 에피소드 기록
  brain.log_episode("국장 첫 수익!", events=[...])
"""
import os
import json
import math
import threading
from datetime import datetime, date

# 주 사용자 엔티티 — contacts.yaml의 primary(role=dad 등)에서 결정.
# config가 없거나 비면 "user"로 폴백 (기억은 엔티티 키로만 묶이므로 이름 자체는 무관).
try:
    from liain import config as _lc
    _PRIMARY = _lc.primary_entity() or "user"
except Exception:
    _lc = None
    _PRIMARY = "user"

# Short-term 자동 chunk 임계 — 신규 raw item 누적 N개 도달 시 background chunk 발동.
# 메인 루프의 2시간 주기 chunk만 있던 시절엔 25KB+로 누적되는 사고가
# 자주 났음 ("06:00에 아침 먹은 얘기를 8시에 잊음"). record_short_term() 끝에
# 임계 체크 + background thread chunk으로 즉시성·응답지연 분리.
_AUTO_CHUNK_THRESHOLD = 5
_AUTO_CHUNK_LOCK = threading.Lock()  # non-blocking acquire — 이미 chunk 중이면 skip

from .paths import BRAIN_DIR
SCORING_LOG_FILE = os.path.join(BRAIN_DIR, "scoring_log.json")
SHORT_TERM_FILE = os.path.join(BRAIN_DIR, "short_term.json")
SHORT_TERM_ARCHIVE_DIR = os.path.join(BRAIN_DIR, "short_term_archive")
MID_TERM_FILE = os.path.join(BRAIN_DIR, "mid_term.json")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _ask_subprocess(prompt, timeout=30):
    """LLM 호출 — liain.llm 라우팅(프로필: 구독CLI/로컬/API). 실패 시 None."""
    try:
        from liain import llm
        return llm.complete("reasoning", "", prompt, timeout=timeout) or None
    except Exception as e:
        print(f"[Brain] LLM 호출 실패: {e}", flush=True)
        return None


def _load(filename):
    path = os.path.join(BRAIN_DIR, filename)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save(filename, data):
    path = os.path.join(BRAIN_DIR, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ─── Importance 채점 시스템 (Park et al. 2023) ─────────────

def _score_importance(memory_text):
    """Claude Code subprocess로 기억의 중요도를 1~10으로 채점.
    1=일상적(양치,식사), 10=인생 전환점(이별,합격).
    """
    prompt = (
        "기억의 중요도를 1~10 숫자 하나만 답해. 설명 없이 숫자만.\n"
        "1=완전 일상적(양치,식사) 3=가벼운 정보(날씨,간단대화) "
        "5=기억할만한것(취향,약속,건강) 7=중요사건(투자결정,직장변화) "
        "9=인생사건(관계변화) 10=절대잊으면안됨(사랑고백,이별,생일)\n\n"
        f"기억: {memory_text}"
    )
    try:
        text = _ask_subprocess(prompt, timeout=15)
        if text:
            for word in text.split():
                try:
                    score = int(word)
                    return max(1, min(10, score))
                except ValueError:
                    continue
        return 5
    except Exception as e:
        print(f"[Brain] importance 채점 실패: {e}", flush=True)
        return 5


def _calculate_recency(timestamp_str):
    """시간 경과에 따른 최신성 점수 (0~1). 지수 감쇠."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        hours_ago = (datetime.now() - ts).total_seconds() / 3600
        # 반감기 72시간 (3일)
        return math.exp(-0.693 * hours_ago / 72)
    except Exception:
        return 0.5


def _log_scoring(memory_text, importance, category, person=""):
    """채점 기록 저장 (대시보드용)."""
    try:
        log = []
        if os.path.exists(SCORING_LOG_FILE):
            with open(SCORING_LOG_FILE) as f:
                log = json.load(f)

        log.append({
            "timestamp": datetime.now().isoformat(),
            "memory": memory_text[:100],
            "importance": importance,
            "category": category,
            "person": person,
        })

        # 최근 200개만 유지
        log = log[-200:]

        with open(SCORING_LOG_FILE, "w") as f:
            json.dump(log, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        print(f"[Brain] 채점 로그 오류: {e}", flush=True)


def get_scoring_log(limit=50):
    """대시보드용 채점 기록 조회."""
    if os.path.exists(SCORING_LOG_FILE):
        with open(SCORING_LOG_FILE) as f:
            log = json.load(f)
        return log[-limit:]
    return []


class Brain:
    def __init__(self):
        self.core = _load("core.json")
        self.semantic = _load("semantic.json")
        self.procedural = _load("procedural.json")
        self.emotional = _load("emotional.json")
        self.opinions = _load("opinions.json")

    def reload(self):
        """파일에서 다시 로드."""
        self.__init__()

    # ─── Context 생성 (시스템 프롬프트에 주입) ──────────────

    def get_context(self, person=_PRIMARY, include_episodes=True):
        """대화 시 시스템 프롬프트에 넣을 기억 컨텍스트."""
        lines = []

        # 0. Short-term — 오늘 한 일 (최우선, 중복 방지)
        st_context = self.get_short_term_context()
        if st_context:
            lines.append(st_context)

        # 1. Core — 핵심 (항상)
        core_person = self.core.get(person, {})
        if core_person:
            lines.append("## 핵심 기억")
            for k, v in core_person.items():
                if isinstance(v, dict):
                    lines.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
                else:
                    lines.append(f"- {k}: {v}")

        # 현재 감정
        emotion = self.core.get("current_emotion", {}).get(person)
        if emotion:
            lines.append(f"- 현재 기분: {emotion}")
        emotion_data = self.emotional.get("current", {}).get(person, {})
        if emotion_data.get("mood") and emotion_data["mood"] != "보통":
            lines.append(f"- 감정 상태: {emotion_data['mood']} (에너지: {emotion_data.get('energy', '?')})")

        # 관계 상태
        rel = self.core.get("relationship_status", {})
        if rel.get("tone"):
            lines.append(f"- 관계 분위기: {rel['tone']} ({rel.get('recent_vibe', '')})")

        # 2. Procedural — 행동 규칙 (항상, 짧게)
        rules = self.procedural.get("conversation_rules", [])
        patterns = self.procedural.get(f"{person}_patterns", {})
        style = self.procedural.get("lian_optimal_style", {})
        if rules or patterns:
            lines.append("\n## 행동 패턴")
            for r in rules[:5]:
                lines.append(f"- {r}")
            if patterns.get("기분좋을때"):
                lines.append(f"- 기분 좋을 때: {patterns['기분좋을때']}")
            if patterns.get("피곤할때"):
                lines.append(f"- 피곤할 때: {patterns['피곤할때']}")
            now_hour = datetime.now().hour
            if 5 <= now_hour < 11 and style.get("아침"):
                lines.append(f"- 지금 스타일: {style['아침']}")
            elif 21 <= now_hour or now_hour < 2:
                if style.get("밤"):
                    lines.append(f"- 지금 스타일: {style['밤']}")

        # 3. 감정 트리거 (짧게)
        triggers = self.emotional.get("triggers", {})
        if triggers:
            lines.append("\n## 감정 트리거")
            for trigger, info in list(triggers.items())[:5]:
                lines.append(f"- '{trigger}' → {info.get('reaction', '')} ({info.get('note', '')})")

            # 4. 세계관 (confidence 높은 순, 상위 5개)
        worldview = self.opinions.get("worldview", {})
        if worldview:
            top_wv = sorted(worldview.items(), key=lambda x: x[1].get("confidence", 0), reverse=True)[:5]
            lines.append("\n## 리안의 세계관 (대화에 반영해)")
            for topic, wv in top_wv:
                lines.append(f"- {topic}: {wv.get('opinion', '')} (확신도 {wv.get('confidence', 0)}/10)")

        # 5. 신념 (confidence 높은 순)
        beliefs = self.opinions.get("beliefs", {})
        if beliefs:
            top_bl = sorted(beliefs.items(), key=lambda x: x[1].get("confidence", 0), reverse=True)[:5]
            lines.append("\n## 리안의 신념 (가치관으로 대화에 자연스럽게 녹여)")
            for topic, bl in top_bl:
                lines.append(f"- {bl.get('belief', '')} (확신도 {bl.get('confidence', 0)}/10)")

        # 6. 최근 에피소드 (importance 높은 순, 최근 5일)
        if include_episodes:
            episodes = self._get_recent_episodes(days=5)
            # importance 순 정렬
            episodes.sort(key=lambda e: e.get("importance", 5), reverse=True)
            if episodes:
                lines.append("\n## 최근 기억")
                for ep in episodes[:5]:  # 상위 5개만
                    imp = ep.get("importance", 5)
                    star = "⭐" if imp >= 7 else ""
                    lines.append(f"- {star}{ep.get('date', '?')}: {ep.get('title', '?')} (중요도 {imp})")
                    if ep.get("lesson"):
                        lines.append(f"  교훈: {ep['lesson']}")

        return "\n".join(lines) if lines else ""

    def get_unified_context(self, include_episodes=True):
        """단일 인격 통합 컨텍스트 — person partition 없음.

        모든 사람(core/semantic/emotional의 person key)을 한 덩어리로 노출.
        리안의 행동 규칙·세계관·신념·에피소드는 공통으로 1회 노출.

        호출자(1:1, 단톡)는 이 위에 '지금 누구와 어디서 대화 중인지'(페르소나)만
        얹는다. 메모리는 동일 — 단톡에서 안 사실은 1:1에서도, 1:1에서 안 사실은
        단톡에서도 같이 알고 있다.
        """
        lines = []

        # 0. Short-term — 오늘 한 일
        st_context = self.get_short_term_context()
        if st_context:
            lines.append(st_context)

        # 1. 사람들 — entity_records (P2.1) 기반.
        # 옛 semantic.json (person dict)는 폐기. records.json의 entity-tagged record를
        # entity별로 그룹핑해 노출. pinned + importance 순 정렬.

        # P2.1c: entity_records 기반. P2.3: valid_at=now 필터 — 만료된(invalidated)
        # record 자동 제외. "지금 무엇이 참" 만 노출, 옛 사실은 retrieval에서 X 시점
        # 질의 시 별도 호출.
        try:
            from . import records as _er
            from datetime import datetime as _dt
            _records = _er.all_records(valid_at=_dt.now().isoformat(timespec="seconds"))
        except Exception as e:
            print(f"[get_unified_context] entity_records 로드 실패: {e}", flush=True)
            _records = []

        # entity 별 그룹핑 + 잡음 (importance < 3) skip
        by_entity: dict[str, list] = {}
        for r in _records:
            if r.get("importance", 5) < 3:
                continue
            for ent in r.get("entities") or []:
                by_entity.setdefault(ent, []).append(r)

        # all_persons 갱신 — records의 entity와 emotional 합집합
        emo_persons = list(self.emotional.get("current", {}).keys())
        all_persons = list(dict.fromkeys(list(by_entity.keys()) + emo_persons))

        people_sections = []
        # P3.2: decay weight 기반 정렬 (importance × exp(-λ·age) × log(recall+2))
        try:
            from . import records as _er
            _now_iso = _dt.now().isoformat(timespec="seconds") if "_dt" in dir() else None
            _weight = lambda r: _er.decay_weight(r, now_iso=_now_iso)
        except Exception:
            _weight = lambda r: float(r.get("importance", 5) or 5)

        _surfaced_ids: list[str] = []
        for person in all_persons:
            person_lines = []
            person_records = by_entity.get(person, [])
            # 정렬: pinned 먼저, 그다음 decay weight 큰 순
            person_records.sort(key=lambda r: (not r.get("pinned"), -_weight(r)))
            # entity별 상위 30개만 노출 (컨텍스트 폭증 방지)
            # fact text에 "오늘"/"이번 주" 같은 상대 시간 표현이 그대로 박힌 경우 많음
            # → timestamp humanize prefix로 그 시점 명시 (LLM이 현재 시점으로 오해 방지)
            try:
                from time_context import humanize_timestamp as _ht
                from datetime import datetime as _dt_now
                _now = _dt_now.now()
            except Exception:
                _ht = None
                _now = None
            for r in person_records[:30]:
                # source ts 우선, 없으면 created_at
                _src_list = r.get("sources") or []
                _ts = (_src_list[0].get("ts") if _src_list else "") or r.get("created_at") or ""
                _prefix = ""
                if _ts and _ht and _now:
                    try:
                        _prefix = f"[{_ht(_ts, now=_now)}] "
                    except Exception:
                        _prefix = f"[{_ts[:10]}] " if _ts else ""
                elif _ts:
                    _prefix = f"[{_ts[:10]}] "
                person_lines.append(f"- {_prefix}{r['text']}")
                if r.get("id"):
                    _surfaced_ids.append(r["id"])
            # emotional.current 슬롯 (P0.4 메모대로 single source)
            emo = self.emotional.get("current", {}).get(person, {})
            if emo.get("mood") and emo["mood"] != "보통":
                person_lines.append(f"- 현재 감정: {emo['mood']} (에너지: {emo.get('energy', '?')})")
            if emo.get("note"):
                person_lines.append(f"- 감정 메모: {emo['note']}")
            if person_lines:
                people_sections.append(f"### {person}\n" + "\n".join(person_lines))

        if people_sections:
            lines.append("## 리안이 알고 있는 사람들")
            lines.extend(people_sections)

        # 2. 관계 분위기 (전체 공통)
        rel = self.core.get("relationship_status", {})
        if rel.get("tone"):
            lines.append(f"\n## 관계 분위기\n- {rel['tone']} ({rel.get('recent_vibe', '')})")

        # 3. 행동 패턴 — rules + 모든 person patterns 통합
        rules = self.procedural.get("conversation_rules", [])
        style = self.procedural.get("lian_optimal_style", {})
        person_patterns = {k: v for k, v in self.procedural.items()
                           if k.endswith("_patterns") and isinstance(v, dict)}
        if rules or person_patterns:
            lines.append("\n## 행동 패턴")
            for r in rules[:5]:
                lines.append(f"- {r}")
            for p_key, patterns in person_patterns.items():
                person_name = p_key.replace("_patterns", "")
                for situation, response in patterns.items():
                    lines.append(f"- {person_name} {situation}: {response}")
            now_hour = datetime.now().hour
            if 5 <= now_hour < 11 and style.get("아침"):
                lines.append(f"- 지금 스타일: {style['아침']}")
            elif (21 <= now_hour or now_hour < 2) and style.get("밤"):
                lines.append(f"- 지금 스타일: {style['밤']}")

        # 4. 감정 트리거 (공통)
        triggers = self.emotional.get("triggers", {})
        if triggers:
            lines.append("\n## 감정 트리거")
            for trigger, info in list(triggers.items())[:5]:
                lines.append(f"- '{trigger}' → {info.get('reaction', '')} ({info.get('note', '')})")

        # 5. 세계관 (공통)
        worldview = self.opinions.get("worldview", {})
        if worldview:
            top_wv = sorted(worldview.items(), key=lambda x: x[1].get("confidence", 0), reverse=True)[:5]
            lines.append("\n## 리안의 세계관 (대화에 반영해)")
            for topic, wv in top_wv:
                lines.append(f"- {topic}: {wv.get('opinion', '')} (확신도 {wv.get('confidence', 0)}/10)")

        # 6. 신념 (공통)
        beliefs = self.opinions.get("beliefs", {})
        if beliefs:
            top_bl = sorted(beliefs.items(), key=lambda x: x[1].get("confidence", 0), reverse=True)[:5]
            lines.append("\n## 리안의 신념 (가치관으로 대화에 자연스럽게 녹여)")
            for topic, bl in top_bl:
                lines.append(f"- {bl.get('belief', '')} (확신도 {bl.get('confidence', 0)}/10)")

        # 7. 최근 에피소드 (공통)
        if include_episodes:
            episodes = self._get_recent_episodes(days=5)
            episodes.sort(key=lambda e: e.get("importance", 5), reverse=True)
            if episodes:
                lines.append("\n## 최근 기억")
                for ep in episodes[:5]:
                    imp = ep.get("importance", 5)
                    star = "⭐" if imp >= 7 else ""
                    lines.append(f"- {star}{ep.get('date', '?')}: {ep.get('title', '?')} (중요도 {imp})")
                    if ep.get("lesson"):
                        lines.append(f"  교훈: {ep['lesson']}")

        # P3.2: 노출된 record들의 recall_count +1 (decay weight 보강용)
        if _surfaced_ids:
            try:
                from . import records as _er
                _er.bump_recall(_surfaced_ids)
            except Exception as _e:
                print(f"[get_unified_context] bump_recall 실패: {_e}", flush=True)

        # Phase 3: Reflection insights — raw episode 대신 추상화된 통찰 노출
        # (Park 2023 Generative Agents: high-level reflection이 raw보다 더 인지적)
        try:
            import os as _os, json as _json
            _rec_file = _os.path.join(_os.path.dirname(__file__), "brain", "records.json")
            if _os.path.exists(_rec_file):
                with open(_rec_file) as _f:
                    _data = _json.load(_f)
                _all_recs = _data if isinstance(_data, list) else (
                    _data.get("records") or list(_data.values()) if isinstance(_data, dict) else []
                )
                _insights = [
                    r for r in _all_recs
                    if isinstance(r, dict) and any(t in (r.get("tags") or []) for t in ("insight", "reflection"))
                ]
                # records.json schema: created_at/updated_at (ts 필드 없음)
                _insights.sort(
                    key=lambda r: r.get("created_at") or r.get("updated_at") or "",
                    reverse=True,
                )
                if _insights[:5]:
                    lines.append("\n## 최근 추상화된 통찰 (reflection)")
                    for _r in _insights[:5]:
                        _ts = (_r.get("created_at") or _r.get("updated_at") or "")[:10]
                        lines.append(f"- [{_ts}] {_r.get('text', '')}")
        except Exception as _e:
            print(f"[get_unified_context] reflection 로드 실패: {_e}", flush=True)

        return "\n".join(lines) if lines else ""

    def get_semantic_context(self, person=_PRIMARY):
        """특정 사람에 대한 의미 기억 (INFO 대화 시 추가 주입)."""
        data = self.semantic.get(person, {})
        if not data:
            return ""
        lines = [f"## {person} 상세 정보"]
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                lines.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
            else:
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    # ─── 기억 업데이트 ──────────────────────────────────

    def update_semantic(self, person, key, value, auto_score=True):
        """사실 기억 업데이트 + importance 채점."""
        if person not in self.semantic:
            self.semantic[person] = {}

        # importance 채점
        importance = 5
        if auto_score:
            memory_text = f"{person}의 {key}: {value}"
            importance = _score_importance(memory_text)
            _log_scoring(memory_text, importance, "semantic", person)

        self.semantic[person][key] = {
            "value": value,
            "importance": importance,
            "updated": datetime.now().isoformat(),
        } if auto_score else value

        # importance 2 이하는 7일 후 자동 삭제 대상 표시
        if importance <= 2:
            self.semantic[person][key]["expires"] = (datetime.now().replace(hour=0) + __import__('datetime').timedelta(days=7)).isoformat()

        _save("semantic.json", self.semantic)
        print(f"[Brain] semantic: {person}.{key} = {value} (importance={importance})", flush=True)

    def update_core(self, key, value):
        """핵심 기억 업데이트."""
        keys = key.split(".")
        target = self.core
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value
        _save("core.json", self.core)
        print(f"[Brain] core 업데이트: {key} = {value}", flush=True)

    def update_emotion(self, person, mood, note="", energy="보통"):
        """감정 상태 업데이트."""
        if "current" not in self.emotional:
            self.emotional["current"] = {}
        self.emotional["current"][person] = {
            "mood": mood,
            "energy": energy,
            "updated": datetime.now().isoformat(),
            "note": note,
        }
        # 히스토리에도 추가
        if "history" not in self.emotional:
            self.emotional["history"] = []
        self.emotional["history"].append({
            "date": datetime.now().isoformat(),
            "person": person,
            "mood": mood,
            "note": note,
        })
        # 히스토리 100개 제한
        self.emotional["history"] = self.emotional["history"][-100:]
        _save("emotional.json", self.emotional)
        print(f"[Brain] emotion 업데이트: {person} = {mood} ({note})", flush=True)

    def add_procedural_rule(self, rule):
        """행동 규칙 추가."""
        rules = self.procedural.get("conversation_rules", [])
        if rule not in rules:
            rules.append(rule)
            self.procedural["conversation_rules"] = rules
            _save("procedural.json", self.procedural)
            print(f"[Brain] procedural 추가: {rule}", flush=True)

    def add_learned_behavior(self, behavior):
        """학습된 행동 추가."""
        learned = self.procedural.get("learned_behaviors", [])
        learned.append({"behavior": behavior, "learned_at": datetime.now().isoformat()})
        self.procedural["learned_behaviors"] = learned[-50:]
        _save("procedural.json", self.procedural)

    # ─── 에피소드 ────────────────────────────────────────

    def log_episode(self, title, events=None, emotion="", lesson=""):
        """오늘의 에피소드 기록 + importance 채점."""
        today = date.today().isoformat()
        ep_file = os.path.join(BRAIN_DIR, "episodes", f"{today}.json")

        if os.path.exists(ep_file):
            with open(ep_file) as f:
                ep = json.load(f)
        else:
            ep = {"date": today, "title": title, "events": [], "emotion": "", "lesson": "", "importance": 5}

        if title:
            ep["title"] = title
            # 에피소드 제목으로 importance 채점
            importance = _score_importance(f"에피소드: {title}")
            ep["importance"] = max(ep.get("importance", 0), importance)
            _log_scoring(title, importance, "episode")
        if events:
            ep["events"].extend(events)
        if emotion:
            ep["emotion"] = emotion
        if lesson:
            ep["lesson"] = lesson

        os.makedirs(os.path.dirname(ep_file), exist_ok=True)
        with open(ep_file, "w") as f:
            json.dump(ep, f, ensure_ascii=False, indent=2)
        print(f"[Brain] episode: {today} — {title} (importance={ep.get('importance',5)})", flush=True)

    def add_episode_event(self, event_text):
        """오늘 에피소드에 이벤트 추가."""
        now = datetime.now()
        self.log_episode(
            title="",
            events=[{"time": now.strftime("%H:%M"), "event": event_text}],
        )

    def _get_recent_episodes(self, days=3):
        """최근 N일 에피소드."""
        ep_dir = os.path.join(BRAIN_DIR, "episodes")
        if not os.path.exists(ep_dir):
            return []
        files = sorted([f for f in os.listdir(ep_dir) if f.endswith(".json")], reverse=True)
        episodes = []
        for f in files[:days]:
            path = os.path.join(ep_dir, f)
            with open(path) as fh:
                episodes.append(json.load(fh))
        return episodes

    # ─── 대화 후 자동 기억 처리 ──────────────────────────

    def process_conversation(self, messages, person=_PRIMARY):
        """대화 후 호출. emotion 갱신 + episode 사건 추출. (P2.4c: slim down)

        - fact 추출은 memory_manager.process_message가 한다 (entity_records).
        - 여기는 turn 단위 emotion(current) 갱신과 일별 episode_event 누적만 담당.
        - P2.5 신호 분류기가 emotion 추출을 흡수하면 이 함수는 episode_event만 남기고 폐기.
        """
        if not messages or len(messages) < 2:
            return

        recent = messages[-6:]
        conv_text = "\n".join([
            f"{'상대' if m['role'] == 'user' else '리안'}: {m['content']}"
            for m in recent
        ])

        prompt = (
            "대화를 분석해서 JSON으로만 응답해. 설명 없이 JSON만.\n"
            '{"emotion": "기분", "emotion_note": "이유", '
            '"episode_event": "기억할 사건 또는 빈문자열"}\n'
            "변화 없으면 각 필드 비워.\n\n"
            f"대화:\n{conv_text}"
        )

        try:
            text = _ask_subprocess(prompt, timeout=30)
            if not text:
                return
            start = text.find('{')
            end = text.rfind('}') + 1
            if start >= 0 and end > start:
                data = json.loads(text[start:end])

                emotion = data.get("emotion", "")
                if emotion and emotion != "보통":
                    self.update_emotion(person, emotion, data.get("emotion_note", ""))

                event = data.get("episode_event", "")
                if event:
                    self.add_episode_event(event)

        except Exception as e:
            print(f"[Brain] 대화 처리 오류: {e}", flush=True)

    # ─── 전체 기억 조회 (대시보드용) ──────────────────────

    # ─── Short-term Memory (오늘 한 일) ──────────────────

    def record_short_term(self, content, action_type="general", importance=None):
        """오늘의 행동/대화 기록. 매 대화 후 호출."""
        st = self._load_short_term()
        today = date.today().isoformat()
        if st.get("date") != today:
            st = {"date": today, "items": []}

        # importance 채점 (안 넘어왔으면)
        if importance is None:
            importance = _score_importance(content)
            _log_scoring(content, importance, "short_term")

        st["items"].append({
            "time": datetime.now().strftime("%H:%M"),
            "content": content,
            "type": action_type,
            "importance": importance,
        })

        # Flashbulb: importance 8+ → 바로 Long-term
        if importance >= 8:
            print(f"[Brain] Flashbulb! '{content[:30]}' (importance={importance}) → 바로 Long-term", flush=True)
            self.log_episode(
                title=content[:50],
                events=[{"time": datetime.now().strftime("%H:%M"), "event": content}],
            )

        _save("short_term.json", st)

        # 자동 chunk 임계 체크 (background thread, 응답 지연 없음).
        self._maybe_auto_chunk()

    def _maybe_auto_chunk(self):
        """신규 raw item이 임계 도달 시 background chunk. 이미 chunk 중이면 skip."""
        if not _AUTO_CHUNK_LOCK.acquire(blocking=False):
            return  # 다른 thread가 chunk 중

        def _do():
            try:
                st = self._load_short_term()
                if st.get("date") != date.today().isoformat():
                    return
                items = st.get("items", [])
                last_chunk_idx = st.get("last_chunk_idx", 0)
                new_count = len(items) - last_chunk_idx
                if new_count < _AUTO_CHUNK_THRESHOLD:
                    return
                self.chunk_short_term()
            finally:
                _AUTO_CHUNK_LOCK.release()

        threading.Thread(target=_do, daemon=True).start()

    def chunk_short_term(self):
        """누적된 short-term items를 시간대별 chunk로 압축.

        Drift 가드: items[last_chunk_idx:] (raw)만 source로 사용.
        chunk을 다시 chunk하지 않음 — items 배열엔 raw가 그대로 남고,
        last_chunk_idx만 전진. "요약의 요약" 안티패턴 방지.
        """
        st = self._load_short_term()
        if st.get("date") != date.today().isoformat():
            return

        items = st.get("items", [])
        if not items:
            return

        chunks = st.get("chunks", [])
        last_chunk_idx = st.get("last_chunk_idx", 0)
        # raw에서만 chunk — chunks[]는 source로 쓰지 않음 (drift 가드)
        new_items = items[last_chunk_idx:]

        if len(new_items) < 3:
            return  # 3개 미만이면 아직 chunk 불필요

        items_text = "\n".join([
            f"- {i['time']} [{i['type']}] {i['content']}" for i in new_items
        ])

        prompt = (
            "아래 시간별 기록을 2~4문장으로 요약해. 핵심 사실과 감정 위주로, 자연스러운 한국어로. "
            "중요한 약속, 요청, 감정 변화는 반드시 포함. 설명 없이 요약만.\n\n"
            f"{items_text}"
        )

        try:
            summary = _ask_subprocess(prompt, timeout=30)
            if summary:
                period = f"{new_items[0]['time']}~{new_items[-1]['time']}"
                chunks.append({
                    "period": period,
                    "summary": summary,
                    "chunked_at": datetime.now().isoformat(),
                })
                st["chunks"] = chunks
                st["last_chunk_idx"] = len(items)
                _save("short_term.json", st)
                print(f"[Brain] chunk: {period} ({len(new_items)}개 → 요약)", flush=True)
        except Exception as e:
            print(f"[Brain] chunk 실패: {e}", flush=True)

    def get_short_term_context(self):
        """시스템 프롬프트에 주입할 오늘 하루 컨텍스트. chunk 우선, 미처리분은 raw."""
        st = self._load_short_term()
        if st.get("date") != date.today().isoformat():
            return ""

        items = st.get("items", [])
        chunks = st.get("chunks", [])
        last_chunk_idx = st.get("last_chunk_idx", 0)

        if not items and not chunks:
            return ""

        lines = ["## 오늘 하루 (이미 한 것은 다시 하지 마!)"]

        # Chunks: 시간대별 요약
        for c in chunks:
            lines.append(f"[{c['period']}] {c['summary']}")

        # 아직 chunk 안 된 최근 항목
        recent = items[last_chunk_idx:]
        if recent:
            if chunks:
                lines.append(f"\n[{recent[0]['time']}~지금]")
            for item in recent:
                lines.append(f"- {item['time']} {item['content']}")

        # Mid-term 패턴
        mt = self._load_mid_term()
        patterns = [m for m in mt.get("items", []) if m.get("count", 0) >= 2]
        if patterns:
            lines.append("\n## 최근 패턴 (대화에 활용해)")
            for p in patterns[:5]:
                lines.append(f"- {p['content']} ({p['count']}회)")

        return "\n".join(lines)

    def _load_short_term(self):
        if os.path.exists(SHORT_TERM_FILE):
            with open(SHORT_TERM_FILE) as f:
                return json.load(f)
        return {"date": None, "items": []}

    def _load_mid_term(self):
        if os.path.exists(MID_TERM_FILE):
            with open(MID_TERM_FILE) as f:
                return json.load(f)
        return {"items": []}

    # ─── Mid-term Memory (1~2주 패턴) ─────────────────────

    def _find_similar_in_midterm(self, content, mid_items):
        """Mid-term에 비슷한 항목 찾기 (단순 키워드 매칭)."""
        content_lower = content.lower()
        for item in mid_items:
            item_lower = item.get("content", "").lower()
            # 핵심 단어 3자 이상 겹치면 유사
            content_words = set(w for w in content_lower.split() if len(w) >= 2)
            item_words = set(w for w in item_lower.split() if len(w) >= 2)
            overlap = content_words & item_words
            if len(overlap) >= 1:
                return item
        return None

    def consolidate_daily(self):
        """자정 정리: Short-term → Mid/Long 승격 + 패턴 감지 + 망각.

        Flashbulb (importance 8+): 이미 record_short_term에서 즉시 Long-term.
        패턴 감지: 어제/Mid-term에 비슷한 거 있으면 count ↑.
        반복 3회+: Long-term 승격.
        importance 1~2: 소멸.
        """
        print("[Brain] 일일 기억 정리 시작...", flush=True)
        today = date.today().isoformat()
        st = self._load_short_term()
        mt = self._load_mid_term()
        mid_items = mt.get("items", [])

        # 어제 아카이브 로드
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        yesterday_file = os.path.join(SHORT_TERM_ARCHIVE_DIR, f"{yesterday}.json")
        yesterday_items = []
        if os.path.exists(yesterday_file):
            with open(yesterday_file) as f:
                yd = json.load(f)
                yesterday_items = yd.get("items", [])

        promoted = 0
        decayed = 0

        for item in st.get("items", []):
            content = item.get("content", "")
            importance = item.get("importance", 3)

            # 이미 Flashbulb 처리된 건 스킵
            if importance >= 8:
                continue

            # Mid-term에 비슷한 거 있나?
            similar_mid = self._find_similar_in_midterm(content, mid_items)
            if similar_mid:
                similar_mid["count"] = similar_mid.get("count", 1) + 1
                similar_mid["last_seen"] = today
                # 3회+ → Long-term 승격 (records로 — get_unified_context가 읽는 표준 store.
                # 과거엔 update_semantic(죽은 store)로 승격해 컨텍스트에 안 떴던 버그 수정)
                if similar_mid["count"] >= 3:
                    cnt = similar_mid["count"]
                    print(f"[Brain] 패턴 승격: '{content[:30]}' ({cnt}회) → records(long-term)", flush=True)
                    # 인물 식별 — contacts.yaml의 alias로만 매핑. 기본=주 사용자.
                    ent = _PRIMARY
                    _a2e = _lc.alias_to_entity() if _lc else {}
                    for nm, key in _a2e.items():
                        if key != _PRIMARY and nm in content:
                            ent = key; break
                    try:
                        from . import records as _er
                        from datetime import datetime as _dt
                        _er.add(
                            content, entities=[ent],
                            tags=["pattern", "promoted", "long_term"],
                            importance=6, pinned=True,
                            sources=[{"channel": "consolidation",
                                      "ts": _dt.now().isoformat(timespec="seconds"),
                                      "msg_id": f"pattern_{today}"}],
                        )
                    except Exception as e:
                        print(f"[Brain] records 승격 실패, semantic fallback: {e}", flush=True)
                        self.update_semantic(_PRIMARY, similar_mid.get("key", content[:20]),
                                             content, auto_score=False)
                    mid_items.remove(similar_mid)
                    promoted += 1
                continue

            # 어제에 비슷한 거 있나? (2일 연속)
            similar_yesterday = None
            for yi in yesterday_items:
                yi_lower = yi.get("content", "").lower()
                if any(w in yi_lower for w in content.lower().split() if len(w) >= 2):
                    similar_yesterday = yi
                    break

            if similar_yesterday:
                # 패턴 시작! Mid-term에 등록
                mid_items.append({
                    "content": content,
                    "key": content[:20],
                    "pattern_type": "repeated",
                    "count": 2,
                    "first_seen": yesterday,
                    "last_seen": today,
                    "note": f"2일 연속: {similar_yesterday.get('content', '')[:30]} → {content[:30]}",
                })
                print(f"[Brain] 패턴 감지: '{content[:30]}' (2일 연속) → Mid-term", flush=True)
                continue

            # importance 3~7 → Mid-term
            if importance >= 3:
                mid_items.append({
                    "content": content,
                    "key": content[:20],
                    "pattern_type": "single",
                    "count": 1,
                    "first_seen": today,
                    "last_seen": today,
                    "importance": importance,
                })
                continue

            # importance 1~2 → 소멸
            decayed += 1

        # Mid-term 정리: 2주 안 언급된 건 삭제
        cutoff = (date.today() - timedelta(days=14)).isoformat()
        before = len(mid_items)
        mid_items = [m for m in mid_items if m.get("last_seen", "") >= cutoff]
        mid_decayed = before - len(mid_items)

        # 저장
        mt["items"] = mid_items[-100:]  # 최대 100개
        _save("mid_term.json", mt)

        # Short-term 아카이브 (어제 비교용)
        os.makedirs(SHORT_TERM_ARCHIVE_DIR, exist_ok=True)
        archive_file = os.path.join(SHORT_TERM_ARCHIVE_DIR, f"{today}.json")
        with open(archive_file, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)

        # 오래된 아카이브 삭제 (3일만 유지)
        for fname in os.listdir(SHORT_TERM_ARCHIVE_DIR):
            if fname.endswith(".json") and fname[:10] < (date.today() - timedelta(days=3)).isoformat():
                os.remove(os.path.join(SHORT_TERM_ARCHIVE_DIR, fname))

        # Short-term 리셋
        _save("short_term.json", {"date": None, "items": []})

        print(f"[Brain] 정리 완료: 승격 {promoted}, 소멸 {decayed}, Mid 만료삭제 {mid_decayed}", flush=True)
        return {"promoted": promoted, "decayed": decayed, "mid_decayed": mid_decayed}

    def cleanup_expired(self):
        """만료된 기억 자동 삭제 (importance 2 이하, 기한 지난 것)."""
        now = datetime.now().isoformat()
        cleaned = 0
        for person, data in self.semantic.items():
            if person.startswith("_"):
                continue
            expired_keys = []
            for k, v in data.items():
                if isinstance(v, dict) and v.get("expires") and v["expires"] < now:
                    expired_keys.append(k)
            for k in expired_keys:
                print(f"[Brain] 만료 삭제: {person}.{k} (importance={data[k].get('importance',0)})", flush=True)
                del data[k]
                cleaned += 1
        if cleaned:
            _save("semantic.json", self.semantic)
            print(f"[Brain] {cleaned}개 기억 만료 삭제", flush=True)
        return cleaned

    def get_all_memories(self):
        """대시보드용 전체 기억."""
        return {
            "core": self.core,
            "semantic": self.semantic,
            "procedural": self.procedural,
            "emotional": self.emotional,
            "episodes": self._get_recent_episodes(days=14),
        }

    def get_emotion_history(self, person=_PRIMARY, limit=30):
        """감정 히스토리."""
        history = self.emotional.get("history", [])
        return [h for h in history if h.get("person") == person][-limit:]
