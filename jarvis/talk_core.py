"""PC konuşma çekirdeği: the smallest text conversation loop from the PC window.

One step: clock -> one log read -> held keys -> PC items -> keyed "in:pc:<id>"
line -> brain asked once (only when "reply:<id>" is absent) -> keyed reply
line -> ack -> view model. The core sees only the records of jarvis.talk_boundary
and does no shape checks of its own. Every port call first spends one unit of
the step budget; the last unit is kept for the view. A restart is a new Core:
its first read rebuilds the state.
"""

from jarvis.talk_boundary import (
    ABSENT, EXISTS, FAILED_TEXT, OK, REFUSED_TEXT, UNKNOWN, LogLine, Refused,
)

NO_BUDGET = Refused("budget")


def start(ports, config):
    """A Core, or the Refused config value as it came."""
    return config if config.refused else Core(ports, config)


class Core:
    def __init__(self, ports, config):
        self.p, self.cfg = ports, config
        self.lines, self.held, self.left, self.now = {}, set(), 0, 0

    def _call(self, fn, *args, need=2):
        """need: units this call and what must follow it in the step require."""
        if self.left < need:
            return NO_BUDGET
        self.left -= 1
        return fn(*args)

    def _write(self, line):
        """OK / EXISTS when the key holds a line, else something to retry."""
        got = self._call(self.p.log.append, line)
        if got is OK:
            self.lines[line.key] = line
            return OK
        if got is not EXISTS and got is not UNKNOWN:
            return got
        return self._lookup(line.key)

    def _lookup(self, key):
        got = self._call(self.p.log.get, key)
        if got is UNKNOWN:
            self.held.add(key)
        elif got is not ABSENT and got is not NO_BUDGET:
            self.lines[key] = got
            self.held.discard(key)
            return EXISTS
        return got

    def step(self):
        """One bounded pass; returns the view model it published."""
        self.left = self.cfg.budget
        tick = self._call(self.p.clock.now)
        read = UNKNOWN if tick is UNKNOWN or tick is NO_BUDGET else self._call(self.p.log.read)
        if read is not UNKNOWN and read is not NO_BUDGET:
            self.now = tick.now
            for line in read:
                self.lines.setdefault(line.key, line)
            for key in sorted(self.held):
                if key in self.lines:
                    self.held.discard(key)
                elif self._lookup(key) is ABSENT:
                    self.held.discard(key)
            items = self._call(self.p.inbox.take, self.cfg.max_pc)
            if items is not UNKNOWN and items is not NO_BUDGET:
                for item in items:
                    self._item(item)
        model = self.model()
        self._call(self.p.view.publish, model, need=1)
        return model

    def _item(self, item):
        key = "in:pc:" + item.item_id
        if key in self.held:
            return
        if key not in self.lines:
            if item.refused:
                line = LogLine(key, "refused", REFUSED_TEXT, self.now)
            else:
                line = LogLine(key, item.kind, item.text, self.now)
            if self._write(line) is not OK and key not in self.lines:
                return
        stored = self.lines[key]
        end = key if stored.kind == "refused" else "reply:" + item.item_id
        if end not in self.lines:
            self._answer(stored, end)
        if end in self.lines:
            self._call(self.p.inbox.ack, item.handle)

    def _answer(self, stored, rkey):
        if rkey in self.held or self._lookup(rkey) is not ABSENT:
            return
        question = stored.text
        if stored.kind == "retry":
            target = self.lines.get("in:pc:" + stored.text)
            failed = self.lines.get("reply:" + stored.text)
            if target is None or target.kind != "text" or failed is None or failed.kind != "failed":
                self._write(LogLine(rkey, "refused", REFUSED_TEXT, self.now))
                return
            question = target.text
        # ask + reply append + its get + ack + view: an answer is never asked for and then dropped
        got = self._call(self.p.brain.ask, question, need=5)
        if got is NO_BUDGET:
            return
        if got is UNKNOWN or got.refused:
            self._write(LogLine(rkey, "failed", FAILED_TEXT, self.now))
        else:
            self._write(LogLine(rkey, "reply", got.text, self.now))  # EXISTS: stored one wins

    def model(self):
        """Plain mappings: one turn per in line, oldest first."""
        turns = []
        for line in sorted(self.lines.values(), key=lambda x: (x.time, x.key)):
            if line.key.startswith("in:pc:"):
                ident = line.key[6:]
                end = self.lines.get(line.key if line.kind == "refused" else "reply:" + ident)
                turns.append({
                    "id": ident, "kind": line.kind, "text": line.text,
                    "state": "pending" if end is None else end.kind,
                    "reply": None if end is None else end.text,
                })
        return {"turns": turns, "held": sorted(self.held)}
