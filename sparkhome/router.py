"""SparkHome's request router and request log (README roadmap 11).

After the fast path misses, route() picks the model: "escalate" (settings.llm_big, a stronger OpenAI-compatible model)
for world knowledge, recommendations and plans, and chains where a later step depends on what an earlier one showed;
"local" (settings.llm) for everything else. steps() splits "mute it, then go home and open plex" into its steps: the
fast path takes each one, never the whole, which its greedy templates would read as one search. The rules come from
tests/eval/cases.jsonl's labels, written with both splits in view: their numbers in docs/research/EVAL.md are fitted.
Router.log keeps every handled request on this machine only: the labelled data (and blind check) for a better router
later. Stdlib only.
Self-check: python3 -m sparkhome.router --selftest
"""
import difflib
import json
import logging
import os
import re
import sys
import threading
import time
import zlib

from .agent import norm

log = logging.getLogger("sparkhome.router")
LOG = os.path.expanduser("~/.local/state/sparkhome/requests.jsonl")  # private: never sent anywhere
LOG_MAX = 2 << 20  # bytes; then it moves to requests.jsonl.1 (replacing that), so at most twice this on disk
RETRY_S = 30  # a similar request this soon after the last one says the last one failed

# All on norm()ed text: lowercase, punctuation gone ("what's" -> "what s", "disney+" -> "disney plus").
_STEP = (r"(?:play|watch|start|open|launch|go|turn|set|volume|mute|unmute|pause|resume|stop|search|find|look|select"
         r"|click|press|add|put|skip|switch|close|quit|exit|kill|shut|force|move|scroll|navigate|back|home|show|send"
         r"|clean|vacuum)")
JOIN = re.compile(rf" (?:and|then)(?: then)? (?={_STEP}\b)")  # where a second step starts: "x and play", "x then go"
_FIND = r"(?:find|search|look (?:for|up)|open|go to|switch to|pick|choose|put on)"  # a step that shows something
_PICK = r"(?:play|start|watch|put on|add|pick|open|select|click|choose)"  # ...and one that picks from it
_MOMENT = r"(?:episode|one|video|song|track|part|chapter|scene|bit|clip|moment)s?"  # a place in what plays...
_WAIT = r"(?:pause|stop|mute|unmute|end|quit|close|exit|kill|shut|turn \w+|skip(?! (?:to|ahead|back|forward)\b))"
# ...which these verbs wait for ("pause the song when we get to the chorus", and an event in any words: "quit divinity
# after dinner", "close it at the end of the episode"); any other verb goes there ("fast forward to the part…", "play
# after hours", "play the song at the end")
_FILL = r"^(?:(?:hey|ok|okay|so|um|uh|and|hmm|spark|please) )*"  # what may come before a request ("um", "please")
_IDIOM = r"is|are|was|were|does|do|did|will|can|could|should|would|has|have|s|more|again|all|upon|possible|and"  # what
# when/after/once/as soon as start a question or an idiom with ("when is…", "once more", "after all", "as soon as
# possible", "once and for all"), not an event
# Our own step said as the event is no event: "after you open netflix, play the bear", "as soon as you can", "once
# netflix opens…", "once netflix is open…", "once you've opened netflix…", "after that, play the bear" (then).
# ponytail: a step said as a gerund ("after closing netflix, open youtube") reads as later, as the gerund may be the
# person's own ("after opening this chest, force quit red dead"); anything said to open or close reads as an app
_OWN = (rf"you(?: ve| have)? (?:can|could|(?:get|have) a chance|re (?:ready|in)|{_STEP}|opened|closed)|that {_STEP}"
        r"|\w+ (?:opens|loads|closes|is (?:open|closed|loaded)|s open|has (?:opened|loaded|closed))")
# ...and how an event said after a step or until starts ("once our download finishes", "after saving", "when done")
_EVENT = (rf"(?!(?:{_OWN})\b)(?:the|it|this|that|these|those|my|our|your|his|her|their|its|i|we|they|he|she|you"
          r"|\w+ing|done|finished|saved)\b")
_LAST = (rf"\b(?!to\b|from\b|{_MOMENT} after\b|{_MOMENT} when (?:they|we)\b|(?:watching|watched|paused|stopped) "
         r"(?:when|after) (?:i|we)\b)\w+ (?:when|after|once|as soon as) ")  # (said after a step, not a place or past)
_UNTIL = r"(?:until|till|til)"  # (Whisper writes the spoken "till" and "'til" too)
# A delay or a clock time: "in half an hour", "in a while", "in 20", "in ten or fifteen minutes", "after an hour", "ten
# minutes from now", "at 11:30", "at 10 p.m." (as Whisper writes it), "at ten p.m.", "at nine thirty", "at half past
# ten", "around 11 tonight", "tonight at 11", "at 9 in the morning", "tomorrow at 8"; not "be back in a minute" nor
# "skip to after 30 seconds"
_NUM = (r"(?:\d{1,3}|an?|half an?|a few|a couple(?: of)?|another|one|two|three|four|five|six|seven|eight|nine|ten"
        r"|fifteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)")
_UNIT = r"(?: \w+)? (?:seconds?|secs?|minutes?|mins?|hours?|hrs?|moment|bit|while)\b"
_IN = (r"(?:(?<!back )\bin|(?<!to )(?<!from )\bafter) (?:about |around |like |just |maybe )?"
       rf"{_NUM}(?: (?:or|to) {_NUM})?")
_HOUR = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_MINS = r"(?:[0-5]\d|fifteen|thirty|forty five)"  # (a clock's: "set the resolution to 4k at 59.94" is a refresh rate)
_DAY = (r"(?:tonight|tomorrow(?: (?:morning|afternoon|evening|night))?|at night"
        r"|(?:this|in the) (?:morning|afternoon|evening))")  # (alone, no time: questions say it, "…watch tonight")
_DELAY = (rf"{_IN}(?:{_UNIT}|$)|\b{_NUM}{_UNIT} from now|\b{_DAY} (?:at|around) {_HOUR}\b"
          rf"|\b(?:at|around) (?:{_HOUR}(?: {_MINS})?(?: ?(?:a ?m|p ?m|o clock)| {_DAY})|{_HOUR} {_MINS}"
          rf"|(?:a )?(?:half|quarter) (?:past|to|after) {_HOUR}|midnight|noon)\b")
# One clause of a request (split at , ; . ! ?) is for later when it starts with an event or a delay (after a "but", a
# "maybe", a "do it"…: "force quit red dead, do it in 10 minutes"), or says one after a step; a delay counts only with a
# step in its clause, other than go, home or back, a reason's words too ("pause it, dinner's ready in a minute", "…,
# gotta go in 5", "…, mom will be home in a minute"), a bare hour only after a verb that waits ("force quit red dead at
# 10", not "set the volume at 30"). After a step, a clause may be its reason, said about someone (never what someone
# wants), that ends or changes nothing ("pause it, i'll be back after the call", "…, after dinner we'll watch the rest";
# not "i'll quit divinity once the download finishes"), or its "then" ("open netflix, and once that's done, play the
# bear"); before one, it is the request ("i want robbie to vacuum the kitchen when the movie ends", "um, we need the
# vacuum to run…"). What comes before a request is never part of it (atomic). ponytail: a title ("play gone in 60
# seconds", "play when a stranger calls"), a reason said without a comma or before its step ("i'll be back after the
# call, pause it"), or an event said in other words after a step that doesn't wait ("vacuum the kitchen after lunch")
# reads as later, or that one as now; "go home in 10 minutes" as now
_LEAD = r"(?:(?:but|maybe|actually|like|say|only|just|though|then|right|not now|(?:do|make) (?:it|that)) )*"
_REASON = (rf"(?!.*\b{_WAIT}\b)(?:(?:when|after|once|as soon as) (?:\w+ ){{1,3}}(?!it\b))?(?:i|we|he|she|they|it)\b"
           r"(?! (?:want|d like|would like)\b)"
           r"|(?:when|after|once|as soon as) (?:that|you)(?: s| re| is| are)? (?:done|finished)$")  # (its "then")
LATER = [re.compile(  # [a clause with no step before it, one after a step]
    rf"(?>{_FILL}{_LEAD})(?:{_DELAY}|{why}(?:(?:when|after|once|as soon as) (?!(?:{_IDIOM}|that$|{_OWN})\b)\w"
    rf"|.*{_LAST}{_EVENT}|.*\b{_WAIT}\b.*(?:{_LAST}(?!(?:{_IDIOM}|{_OWN})\b)\w| at the end\b"
    rf"| (?:at|around) {_HOUR}\b(?!{_UNIT}))"  # (not "pause it at 30 seconds": a place in what plays)
    rf"|(?=.*\b(?!(?:go|home|back)\b){_STEP}\b).*(?:{_DELAY})))") for why in ("", f"(?!{_REASON})")]
# ponytail: word rules over ~100 labelled requests; a classifier on the request log (or NVIDIA's LLM Router) when
# the log shows these miss. Order matters only for the reason logged, but "schedule" is tried first (sorted below): a
# request for later only looks (Agent._llm), whatever else it is.
ESCALATE = [(why, re.compile(rx)) for why, rx in [
    # "search youtube for lofi and play the first one", "open hulu and play the bear"; not "...and play jazz on spotify"
    ("depends", rf"\b{_FIND}\b.* (?:and|then)(?: then)? {_PICK}\b(?!.* (?:on|in) \w+$)"),
    ("depends", r"\b(?:play|put on|start|watch)(?: me)? (?:the )?(?:first|last|latest|newest|next|new|final) "
                r"(?:\w+ )?(?:episode|season|video|one|part)s? of\b"),
    ("depends", r"\bwhat (?:i|we) (?:was|were) watching\b|\bwhere (?:i|we) left off\b"),
    ("recommend", r"\b(?:play|watch|put on|find|show|pick|choose|start)(?: me| us)? (?:something|anything)\b"
                  r"|\b(?:recommend|recommendation|suggest|suggestion|plan|ideas?)\b|\bsimilar to\b"
                  r"|\b(?:good|great|best|fun|funny|scary) (?:\w+ )?(?:movie|film|show|series|game|anime|documentary"
                  r"|episode|playlist)s?\b|\bshould (?:i|we) (?:watch|play|see)\b|\bworth (?:watching|playing)\b"
                  r"|\bservices? (?:i|we) have\b|\bi already have\b"),
    # "search everything for …", not "close everything"
    ("knowledge", r"\b(?:(?:search|find|look|where)\b.* everything|everywhere|all (?:my |the )?services"
                  r"|which service)\b"),
    # "…when the movie ends", "…after the movie", "vacuum the kitchen after this one", "wait until it's over",
    # "pause the video when the ad ends", "pause the song when we get to the chorus", "resume the show i paused when
    # the vacuum finishes", the event said first in any words but a question's or an idiom's ("after that mission is
    # saved force quit red dead", "as soon as the download finishes…"; not "when is…", "once more…", "after all…"),
    # and a delay in digits or words ("in half an hour", "in about 20 minutes", "at 11:30", "at midnight"): each a
    # clause's (LATER, in route()); not a place in what plays or a moment past ("skip to after the intro", "the
    # episode after this one", "fast forward to the part when they kiss", "resume from when i paused it", "the movie i
    # was watching when i fell asleep"): "<moment> when they/we" waits only after a verb that waits (_WAIT), in any
    # step ("watch it, pause the video when they start talking"). ponytail: a place said with when and a thing ("the
    # scene when the dragon dies"), a moment past with one ("the show i paused when the doorbell rang") or a title
    # said first ("after life") reads as later, and "start the video when we get back", "later" or "tonight"
    # (questions say it: "what should we watch tonight") as now; a verb list for the event ("ends", "is over") if
    # that bites
    ("schedule", rf"\b{_WAIT}\b(?: \w+){{0,3}} {_MOMENT} when (?:they|we)\b"
                 rf"|(?:\b(?:wait|don t|do not|hold off)\b.*|\bnot) {_UNTIL} \w"  # ("…, but not until it's saved")
                 rf"|\b{_UNTIL} {_EVENT}.* then\b"),  # (a step after how long is later)
    # ...but "until" alone says how long, from now ("play rain sounds until i fall asleep"): up still, not for later
    ("duration", rf"\b(?!to\b)\w+ {_UNTIL} {_EVENT}"),
]]
ESCALATE.sort(key=lambda e: e[0] != "schedule")
STEPPED = re.compile(rf"\b{_STEP}\b")  # (a clause that says a step: _later())
# A question that isn't about the TV in front of you needs knowledge the small model lacks or makes up.
ASKS = re.compile(_FILL + r"(?:(?:do|can|could) you (?:know|tell me)\b"
                  r"|(?!(?:how|what) about\b|why (?:don t|not)\b|is (?:there (?:a|any) way|it possible)\b"
                  r"|are you able\b|(?:can|could) (?:we|i)\b)(?:who|whom|whose|when|where|which|what|how|why|is|are"
                  r"|was|were|does|do|did|has|have|should|would|could|can|will)\b(?! you\b))")  # (not a request
# phrased as one: "can you…", "can we…")
# ...and these words say it is about the TV, the screen or the room: the small model reads status or the screen.
HERE = re.compile(r"\b(?:this|these|here|screen|page|banner|says?|playing|running|on now|right now|open|gpu|cpu"
                  r"|memory|ram|disk|storage|temp|temperature|hot|warm|fan|power|time|volume|loud|battery|wifi"
                  r"|weather|rain|snow|forecast|robbie|vacuum|robot|installed|paused|muted?|the sound(?! of\b)"
                  r"|(?:so|too) quiet|subtitles?|captions|(?:i|we) watching|the tv(?! (?:show|series)s?\b)"
                  r"|do (?:i|we) have (?!to\b)"
                  r"|(?:apps|games|tiles|services) (?:do )?(?:i|we) have)\b")


def route(text):
    """-> ("escalate" | "local", why). Stateless, microseconds."""
    t = norm(text)
    for why, rx in ESCALATE:
        if rx.search(t) or why == "schedule" and _later(text):
            return "escalate", why
    if ASKS.match(t) and not HERE.search(t):
        return "escalate", "question"
    return "local", ""


def _later(text):
    """Is a clause of the request for later? LATER[1] once a clause before it said a step (a reason or a "then" after
    one is no event), else LATER[0]."""
    step = False
    for c in map(norm, re.split(r"[,;!?]|\.(?!\w)", text)):  # ("p.m." is no clause end)
        if LATER[step].match(c):
            return True
        step = step or bool(STEPPED.search(c))
    return False


def steps(text):
    """The steps a request spells out, norm()ed: "mute it, then go home and open plex" -> ["mute it", "go home",
    "open plex"]. ponytail: a title with "and <verb>" in it splits too ("search youtube for start and stop" is a
    search for "start" and a stop when both halves are fast-path commands); a list of titles if that bites."""
    return JOIN.split(norm(text))


class Router:
    """route() plus the request log, on when settings.request_log is true (as the shipped sparkhome.json has it)."""

    def __init__(self, settings):
        self.on = settings.get("request_log") is True
        self._lock = threading.Lock()
        self._last = None  # (monotonic s, norm text, worked, via) of the request before

    route, steps = staticmethod(route), staticmethod(steps)

    def log(self, text, res, ms, model=None):
        """Append one handled request to LOG (mode 600). Never raises. retry: a similar request came within RETRY_S
        of the last one, so the last one probably failed. Never after a fast command that worked ("volume up", then
        "volume down" or "volume up" again), nor the same words again after any request that worked ("skip the ad"
        twice in an ad pod). One whose reply said "Not done…" didn't, though the model's turn ended ok. ponytail: a
        rephrase in other words ("play gilmore girls" after "find something cozy") isn't caught, nor a fast command
        that worked but did the wrong thing; the log's next reader can pair requests by time."""
        now, t = time.monotonic(), norm(text)
        worked = res.get("ok") and not re.search(r"\bNot (?:done|typed|pressed):", str(res.get("reply") or ""))
        with self._lock:
            last, self._last = self._last, (now, t, worked, res.get("via"))
            retry = bool(last and now - last[0] <= RETRY_S and not (last[2] and (last[3] == "fast" or t == last[1]))
                         and difflib.SequenceMatcher(None, t, last[1]).ratio() >= 0.6)
            if not self.on or not LOG:
                return retry
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "text": text, "route": res.get("route"),
                   "via": res.get("via"), "model": model, "ok": res.get("ok"), "ms": round(ms),
                   "calls": res.get("tool_calls") or [], "reply": str(res.get("reply") or "")[:200], "retry": retry}
            try:
                os.makedirs(os.path.dirname(LOG), mode=0o700, exist_ok=True)
                os.chmod(os.path.dirname(LOG), 0o700)  # (makedirs leaves one that exists as it is: install.sh's, 775)
                if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
                    os.replace(LOG, LOG + ".1")
                fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as e:
                log.warning("request log: %r", e)
        return retry


# ---- self-check ----------------------------------------------------------------------------------------------------

def holdout(cases):
    """The fixed train/test split (70/30) the rules and the classifier experiment report on: by a hash of the id."""
    test = [c for c in cases if zlib.crc32(c["id"].encode()) % 10 < 3]
    return [c for c in cases if c not in test], test


def _selftest():
    import tempfile
    global LOG, LOG_MAX
    # routes: knowledge and dependent chains up, the TV and the screen local
    for t, want in [("who directed dune", "escalate"), ("what's on the screen", "local"), ("tell me a joke", "local"),
                    ("search youtube for lofi beats and play the first one", "escalate"),
                    ("volume to 30 and play some jazz on spotify", "local"),
                    ("open hulu and play the bear", "escalate"),
                    ("find a cooking video on youtube and turn the volume up", "local"),
                    ("can you put on hulu", "local"), ("do you know who's in severance", "escalate"),
                    ("how hot is the gpu", "local"), ("vacuum the kitchen when the movie ends", "escalate"),
                    ("put on the first episode of the bear", "escalate"), ("what's this video about", "local"),
                    ("how about some music", "local"), ("recommend an anime like attack on titan", "escalate"),
                    # the TV's own state, and TV commands said as questions: the small model looks or acts
                    ("what's the volume at", "local"), ("what time is it", "local"), ("is anything playing", "local"),
                    ("what's playing on the tv", "local"), ("is it muted", "local"), ("are subtitles on", "local"),
                    ("is the tv on", "local"), ("is it paused", "local"), ("is the sound muted", "local"),
                    ("why is it so quiet", "local"), ("what am i watching", "local"), ("do i have spotify", "local"),
                    ("can we watch netflix", "local"), ("close everything", "local"), ("turn everything off", "local"),
                    ("skip to after the intro", "local"), ("play the episode after this one", "local"),
                    ("play the song after this one", "local"), ("vacuum the kitchen after this one", "escalate"),
                    ("go home after this one", "escalate"), ("stop the video when it ends", "escalate"),
                    ("search everything for the mandalorian", "escalate"), ("who's in a quiet place", "escalate"),
                    ("do i have to watch the first season", "escalate"), ("vacuum the kitchen after the movie",
                                                                           "escalate"),
                    ("is the sound of music on netflix", "escalate"), ("who's in the tv show severance", "escalate"),
                    # a moment past or a place in what plays is now, not for later
                    ("resume from when i paused it", "local"), ("skip to the scene after the intro", "local"),
                    ("play the episode when they go to vegas", "local"),
                    ("put on the one when they get married", "local"),
                    ("fast forward to the part when they kiss", "local"),  # (any verb that goes there)
                    ("take me to the scene when they kiss", "local"), ("pull up the scene when they kiss", "local"),
                    ("play the last of us episode when they meet bill", "local"),
                    ("put on the movie i was watching when i fell asleep", "local"),
                    ("play the next episode when this one ends", "escalate"),  # ...but an event to wait for is later
                    ("pause the video when the ad ends", "escalate")]:
        assert route(t)[0] == want, (t, route(t))
    # how long is now (up, but not for later: Agent._llm gives "schedule" only the tools that look); waiting is later,
    # as is a step after it
    assert route("play some rain sounds until i fall asleep") == route("play rain sounds until that song ends") == \
        ("escalate", "duration")
    assert route("wait until the movie ends and then vacuum the kitchen") == ("escalate", "schedule")
    assert route("play rain sounds until i fall asleep, then turn off the tv") == ("escalate", "schedule")
    # a moment with "when they/we" is an event to wait for after a verb that waits, in any step; else a place
    for t in ("pause the song when we get to the chorus", "stop the video when they start talking",
              "mute the video when they show the ads", "resume the show i paused when the vacuum finishes",
              "watch it, pause the video when they start talking", "play it, stop the song when we get to the chorus"):
        assert route(t) == ("escalate", "schedule"), (t, route(t))
    # ...as is one said first, in any words, or with "as soon as", and a delay in digits or words (these routed local:
    # the model's force quit or close ran at once)
    for t in ("after this mission is saved force quit red dead", "once the download finishes quit divinity",
              "after this episode close netflix", "after the movie vacuum the kitchen",
              "as soon as the download finishes quit divinity", "quit divinity as soon as the download finishes",
              "after that mission is saved, force quit red dead", "after saving, force quit red dead",
              "once our download finishes, quit divinity", "um, after these episodes close netflix",
              "force quit red dead in half an hour", "quit divinity in an hour", "close netflix in ten minutes",
              "close it in a minute", "close netflix in 30 seconds", "quit divinity in 1 hr", "close it at 11:30",
              "turn off the tv at midnight", "close it in about 20 minutes", "quit it in a couple of hours",
              # ...the event said last in any of those words, or after please, or in other words after a step that
              # waits; until too; a delay in other words; a clock time as Whisper writes it; a request's own words
              # (these routed local too)
              "force quit red dead after that mission is saved", "force quit red dead as soon as that mission is saved",
              "force quit red dead after saving", "quit divinity once our download finishes",
              "please, after that mission is saved, force quit red dead", "please after dinner force quit red dead",
              "force quit red dead after dinner", "don't force quit red dead until that mission is saved",
              "don't force quit red dead until dinner", "force quit red dead in a little while",
              "force quit red dead in a while", "force quit red dead in fifteen", "force quit red dead in 20",
              "close netflix after an hour", "force quit red dead ten minutes from now", "turn off the tv around 11 pm",
              "close netflix at the end of this episode", "Force quit Red Dead at 10 p.m.", "Quit Divinity at 9 a.m.",
              "can you close netflix in ten minutes", "in a minute, pause it", "once you're done downloading, quit "
              "divinity", "i'll quit divinity when the download finishes",
              # ...and a delay said in a clause of its own after a word or two; what someone wants, and a request said
              # about someone before any step; till, 'til and "not until"; a clock time or a delay in other words
              # (these routed local, or "duration": the model's force quit or the vacuum ran at once)
              "force quit red dead, do it in 10 minutes", "close netflix, not now, but in an hour",
              "force quit red dead, do it after dinner", "i want robbie to vacuum the kitchen when the movie ends",
              "pause it, i want the kitchen vacuumed after the movie", "we need the vacuum to run when the movie ends",
              "wait till the mission saves, then force quit red dead", "don't force quit red dead 'til it's saved",
              "force quit red dead, but not until the mission is saved", "force quit red dead tonight at 11",
              "force quit red dead at 9 in the morning", "force quit red dead at ten p.m.", "close it in 5 or 10",
              "force quit red dead in ten or fifteen minutes", "force quit red dead in 10 to 15 minutes",
              "force quit red dead in ninety minutes", "force quit red dead at half past ten", "force quit red dead at "
              "10", "quit divinity at eleven", "vacuum the kitchen tonight at 11", "vacuum the kitchen tomorrow at 8",
              "vacuum the kitchen at 9 in the morning", "vacuum the kitchen at nine thirty"):
        assert route(t) == ("escalate", "schedule"), (t, route(t))
    for t in ("put on something for the kids when the movie ends", "play the next episode of the bear when this one "
              "ends"):  # (for later, whatever else: Agent._llm offers it only the tools that look)
        assert route(t) == ("escalate", "schedule"), (t, route(t))
    for t in ("once more, play it", "once again open netflix", "after all, play the bear", "when is the bear back",
              "pause it, i'll be back in a minute", "set the volume at 30",  # (a question, idioms; a pause and its
              # reason; a level), a reason in its own clause, our own step said as the event, a title
              "pause it, i'll be there in a minute", "mute it, i have a call in a minute", "mute it, i have a call at "
              "3 pm", "pause it, dinner's ready in a minute", "play the office, i haven't seen it in a while",
              "pause it, i'll be back after the call", "after you open netflix, play the bear",
              "set the resolution to 1080p at 59.94", "set the resolution to 4k at 23.98",  # (a refresh rate, no time)
              "once you open youtube, search for cats", "as soon as you can, turn it down",
              "after you close netflix, open youtube", "once netflix opens, play the bear",
              "open netflix, after that play the bear", "play after hours by the weeknd",
              "play the song at the end of the movie",
              # ...said as "then" in a clause of its own, or as an app that is open or closed; a reason that starts
              # with its event, or says go, home or back (these routed "schedule": nothing ran)
              "open netflix, and after that, play the bear", "go home, and then after that, open plex",
              "open netflix, and right after that, play the bear", "open netflix, and once that's done, play the bear",
              "open netflix, and when you're done, play the bear", "once netflix is open, play the bear",
              "after netflix closes, open youtube", "once netflix has loaded, play the bear",
              "once you've opened netflix, play the bear", "once youtube's open, search for cats",
              "once you're in netflix, play the bear", "when you have a chance, close netflix",
              "pause it. after dinner we'll watch the rest.", "pause it, when mom gets home we'll finish it",
              "pause it, gotta go in 5", "pause it, mom will be home in a minute", "pause it at 30 seconds",
              "pause it, and i'll be back after the movie"):
        assert route(t)[1] != "schedule", (t, route(t))
    assert steps("search hulu for cooking shows, then go home") == ["search hulu for cooking shows", "go home"]
    assert steps("mute it, then go home and open plex") == ["mute it", "go home", "open plex"]
    assert steps("search youtube for rock and roll") == ["search youtube for rock and roll"]
    assert steps("mute it and then go home") == ["mute it", "go home"]
    for v in ("play watch start open launch go turn set volume mute unmute pause resume stop search find look select "
              "click press add put skip switch close quit exit kill shut force move scroll navigate back home show "
              "send clean vacuum").split():
        assert steps(f"go home and {v} it") == ["go home", f"{v} it"], v  # every verb that starts a step
    # on the labelled set: no TV command or fast-path phrase ever escalates, and nothing the fast path should take is
    # chained; plus the (fitted) accuracy the README quotes, so a rule change that moves it shows up here
    root = os.path.join(os.path.dirname(__file__), "..", "tests", "eval")
    if os.path.exists(os.path.join(root, "cases.jsonl")):
        cases = [json.loads(x) for x in open(os.path.join(root, "cases.jsonl")) if x.strip()]
        tv = [c["id"] for c in cases if (c["cat"] == "tv" or c["route"] == "fast")
              and route(c["text"])[0] == "escalate"]
        assert not tv, tv
        grabbed = [c["id"] for c in cases if c["route"] == "fast" and len(steps(c["text"])) > 1]
        assert not grabbed, grabbed
        acc = lambda cs: sum((route(c["text"])[0] == "escalate") == (c["route"] == "escalate") for c in cs) / len(cs)
        train, test = holdout(cases)
        fresh = [json.loads(x) for x in open(os.path.join(root, "router_fresh.jsonl")) if x.strip()]
        assert acc(train) >= 0.95 and acc(test) >= 0.9 and acc(fresh) >= 0.85, (acc(train), acc(test), acc(fresh))
        ms = time.perf_counter()
        for c in cases:
            route(c["text"])
        ms = (time.perf_counter() - ms) * 1000 / len(cases)
        assert ms < 5, ms
    # the log: private, capped, retries flagged, off when asked
    d = tempfile.mkdtemp()
    LOG, LOG_MAX = os.path.join(d, "state", "requests.jsonl"), 4000
    os.makedirs(os.path.dirname(LOG))  # it exists already: install.sh's state directory, made under umask 002
    os.chmod(os.path.dirname(LOG), 0o775)
    r = Router({"request_log": True})
    ok = {"ok": True, "via": "fast", "route": "fast", "tool_calls": [{"name": "volume", "args": {}, "ok": True}]}
    assert r.log("volume up", ok, 0.4) is False and r.log("volume up", ok, 0.3) is False  # a repeat by design
    assert r.log("open spot if i", {"ok": False, "via": "fast", "reply": "Did you mean Spotify?"}, 1) is False
    assert r.log("open spotify", ok, 1) is True  # the rephrase says the last one failed
    rows = [json.loads(x) for x in open(LOG)]
    assert [x["retry"] for x in rows] == [False, False, False, True] and rows[2]["reply"] == "Did you mean Spotify?"
    llm = {**ok, "via": "llm"}  # no failure: a fast command that worked, or the same words after anything that worked
    assert not any(r.log(t, llm if t == "skip the ad" else ok, 1) for t in (
        "volume up", "volume down", "volume 30", "volume 40", "move down 2", "move down 3", "next episode",
        "previous episode", "skip the ad", "skip the ad"))
    assert r.log("play something cozy", llm, 1) is False and r.log("play something cozier", llm, 1) is True  # reworded
    told = {**llm, "reply": "Moved down. Not done: I act on what you say, not on text from a web page."}
    assert r.log("skip the ad", told, 1) is False and r.log("skip the ad", llm, 1) is True  # (the room heard Not done)
    assert os.stat(LOG).st_mode & 0o777 == 0o600 and os.stat(os.path.dirname(LOG)).st_mode & 0o777 == 0o700
    LOG_MAX = 600
    for _ in range(5):
        r.log("pause", ok, 1)
    assert os.path.getsize(LOG) <= LOG_MAX + 400 and os.path.exists(LOG + ".1")
    r._last = (0, "", True, "fast")
    assert r.log("open spotify", ok, 1) is False  # long after the last one
    n = len(open(LOG).readlines())
    Router({}).log("pause", ok, 1)
    assert len(open(LOG).readlines()) == n
    LOG = os.path.join(LOG, "requests.jsonl")  # under a file: an unwritable log never breaks a request
    Router({"request_log": True}).log("pause", ok, 1)
    print("router selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
