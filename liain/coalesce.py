"""메시지 코얼레싱 — 누적 수신 시 '각자 답장' 대신 연계된 한 번만 응답.

사람은 한 생각을 여러 메시지로 나눠 보낸다. 매 메시지마다 답하면 비슷한 답이 여러 번 나간다.
마지막 메시지 후 잠깐(idle) 기다렸다 버스트를 **하나로 합쳐 1회 생성**한다.
- 완결형 메시지는 짧게(빨리 답), 진행중(끊긴 듯)이면 길게 대기하되 30초 상한.
- supersession: 생성/발송 중 새 메시지가 오면 그 발송을 취소하고, 원문을 되돌려 다음 flush가
  합쳐 재검토(문맥 손실 없이).

프레임워크는 생성에 대화 히스토리를 쓰지 않으므로(사실 기억 기반), 버스트의 텍스트를 합쳐
combined 메시지 하나로 콜백을 부른다.
"""
import threading
import time

from .channels.base import IncomingMessage

BATCH_IDLE_COMPLETE = 2.5        # 완결형: 짧게만 기다렸다 답(연속타 잡되 지연 최소)
BATCH_IDLE_INCOMPLETE = 10.0     # 진행중(끊긴 듯): 더 기다림
BATCH_MAX_WAIT = 30.0            # 버스트 첫 메시지 후 이 이상은 무조건 선발송(하드 데드라인)

# 진행중 판별 — 강한 신호만(오판 시 최대 30초 상한 + supersession이 비용 상한).
_CONT_WORDS = ("그리고", "그래서", "근데", "그러고", "그니까", "그래도", "아니", "그럼",
               "이제", "일단", "또", "그러니까", "그러면")
# 연결어미(2글자 이상만) — 바레 "고"/"구"는 명사(친구·문구·야구·대구…) 오탐이라 제외.
_CONT_EOMI = ("며", "면서", "라서", "는데", "은데", "하고", "이고", "라고", "하며", "이며")


def looks_incomplete(text: str) -> bool:
    """다음 메시지가 이어질 것 같은지 — 룰베이스 강한 신호만. 애매하면 False(빨리 답)."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(("...", "…", "..", ",", "·", "、")):     # 트레일링 → 진행중 (종결부호보다 우선)
        return True
    last = t[-1]
    if last in ".!?~)]。！？」』":                          # 단일 종결부호 → 완결
        return False
    if ord(last) >= 0x1F000 or last in "☺♥✨🙂😊😂👍🌙☀🥲🥹😭😅":   # 이모지 → 완결
        return False
    toks = t.split()
    if toks and toks[-1] in _CONT_WORDS:                     # 접속부사로 끝
        return True
    if len(t) >= 3 and t.endswith(_CONT_EOMI):               # 연결어미로 끝(2글자 이상)
        return True
    return False


def _conv_key(msg) -> str:
    return msg.chat_id or msg.sender or msg.entity or "default"


class Coalescer:
    """콜백(_on_message)을 감싸 버스트를 합쳐 1회 호출. poll 콜백으로 submit을 넘긴다."""

    def __init__(self, callback):
        self.callback = callback
        self._lock = threading.Lock()          # 내부 상태(buf/timers/seq/start) 보호
        self._call_lock = threading.Lock()     # 콜백(brain 읽고-쓰기) 직렬화 — H1: 동시 저장 레이스 방지
        self._buf = {}       # cid -> [msg, ...]
        self._timers = {}    # cid -> Timer
        self._seq = {}       # cid -> int (supersession 토큰)
        self._start = {}     # cid -> 버스트 첫 메시지 ts
        self._ctx = threading.local()   # flush 스레드에만 cid/seq 세팅

    def submit(self, msg):
        """poll이 메시지당 호출. 버퍼에 넣고 idle 타이머 (재)무장."""
        cid = _conv_key(msg)
        with self._lock:
            self._seq[cid] = self._seq.get(cid, 0) + 1
            old = self._timers.get(cid)
            now = time.time()
            if old is None:
                self._start[cid] = now                       # 대기 타이머 없음 = 새 버스트
            self._buf.setdefault(cid, []).append(msg)
            # M4: 하드 데드라인 초과면 재무장하지 않음 — 기존 타이머(≤0.5s)가 곧 발화하게 둬
            # 0.5s 미만 간격의 연속타로 flush가 무한 연기되던 것 차단.
            if old is not None and (now - self._start.get(cid, now)) >= BATCH_MAX_WAIT:
                return
            if old is not None:
                old.cancel()
                self._timers.pop(cid, None)
            self._arm(cid, looks_incomplete(msg.text))

    def _arm(self, cid, incomplete):
        now = time.time()
        base = BATCH_IDLE_INCOMPLETE if incomplete else BATCH_IDLE_COMPLETE
        cap_left = (self._start.get(cid, now) + BATCH_MAX_WAIT) - now
        idle = max(0.5, min(base, cap_left))
        t = threading.Timer(idle, self._flush, args=(cid,))
        t.daemon = True
        self._timers[cid] = t
        t.start()

    def _flush(self, cid):
        with self._lock:
            msgs = self._buf.pop(cid, [])
            self._timers.pop(cid, None)
            seq = self._seq.get(cid)
        if not msgs:
            return
        last = msgs[-1]
        combined = IncomingMessage(
            text="\n".join(m.text for m in msgs if m.text),
            sender=last.sender, chat_id=last.chat_id, role=last.role,
            entity=last.entity, is_group=last.is_group,
            is_from_me=last.is_from_me, channel=last.channel, raw=dict(last.raw or {}))
        with self._lock:
            self._start.pop(cid, None)          # 버스트 종료 — 다음 메시지는 새 버스트(L10)
        # H1: 콜백을 전역 직렬화 — brain은 read-modify-write(전체파일)라 스레드 세이프하지 않음.
        # 코얼레싱이 대화별 Timer 스레드로 병렬 실행을 유발 → 서로 다른 대화가 동시에 저장하면
        # last-writer-wins로 한쪽 갱신이 유실됨. poll 루프가 원래 직렬이던 불변식을 여기서 복원.
        with self._call_lock:
            self._ctx.cid = cid
            self._ctx.seq = seq
            try:
                self.callback(combined)
            except Exception as e:
                print(f"[coalesce] flush 오류 {cid}: {e}", flush=True)
                import traceback
                traceback.print_exc()
            finally:
                self._ctx.cid = None
                self._ctx.seq = None

    def is_superseded(self, cid) -> bool:
        """이 flush가 이미 지난 것인지 — 생성 중 같은 대화에 새 메시지가 왔으면 True.

        flush 스레드에서만 참이 될 수 있음(다른 발송 경로는 항상 False).
        """
        try:
            return (getattr(self._ctx, "cid", None) == cid
                    and self._ctx.seq is not None
                    and self._ctx.seq != self._seq.get(cid))
        except Exception:
            return False

    def resubmit(self, msg):
        """supersession으로 취소된 응답의 원문을 버퍼에 되돌림 — 다음 flush가 합쳐 재검토(문맥 보존).

        새 입력이 아니므로 seq는 올리지 않는다. 새 메시지가 이미 타이머를 무장했으면 그걸 쓴다.
        """
        cid = _conv_key(msg)
        with self._lock:
            self._buf.setdefault(cid, []).insert(0, msg)     # 앞에 넣어 순서 보존
            if cid not in self._timers:
                self._arm(cid, False)


__all__ = ["Coalescer", "looks_incomplete", "BATCH_IDLE_COMPLETE",
           "BATCH_IDLE_INCOMPLETE", "BATCH_MAX_WAIT"]
