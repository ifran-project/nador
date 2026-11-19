"""Steps of the F1 handover and the report of one handover: what each step means, and the log line that proves it."""

from dataclasses import dataclass, field

# The steps of the F1 handover, in order (see the package documentation): key -> (title, what the step does).
PROCEDURE: dict[str, tuple[str, str]] = {
    "decision": (
        "CU-CP triggers the handover",
        "The CU-CP accepts the manual command and starts the handover of the UE towards the target cell.",
    ),
    "preparation": (
        "Target DU prepares the UE",
        (
            "F1AP UE CONTEXT SETUP: the target DU creates the UE with a new C-RNTI (and a dedicated preamble with "
            "contention-free random access), and returns its radio configuration in the target cell."
        ),
    ),
    "command": (
        "CU-CP sends the handover command",
        (
            "RRCReconfiguration with reconfigurationWithSync, sent to the UE through the source DU: target cell, new "
            "C-RNTI, preamble, configuration. The UE leaves the source cell when it receives it."
        ),
    ),
    "preamble": (
        "Target DU receives the preamble of the UE (Msg1)",
        "The UE synchronised on the target cell and sent its random access preamble; the target DU detected it.",
    ),
    "response": (
        "Target DU sends the random access response (Msg2)",
        "Timing advance and uplink grant for the UE. A UE that does not decode it sends its preamble again.",
    ),
    "completion": (
        "UE confirms on the target cell (RRCReconfigurationComplete)",
        (
            "The UE sent RRCReconfigurationComplete through the target DU, and the CU-CP received it: the UE is on the "
            "target cell. This is the success criterion of the handover."
        ),
    ),
    "release": (
        "CU-CP releases the UE on the source DU",
        "F1AP UE CONTEXT RELEASE COMMAND to the source DU: end of the handover.",
    ),
}

MARKS = {True: "x", False: " ", None: "?"}


@dataclass
class Step:
    """One step of the handover: whether a log line proves it (None: these logs cannot tell), and which one."""

    key: str
    ok: bool | None
    detail: str = ""
    evidence: str = ""

    @property
    def title(self) -> str:
        """Short name of the step."""
        return PROCEDURE[self.key][0]

    @property
    def meaning(self) -> str:
        """What the step does in the procedure."""
        return PROCEDURE[self.key][1]


@dataclass
class HandoverReport:
    """Result of a handover: steps reached, diagnosis, and what happened next."""

    implementation: str
    ue: str
    source: str
    target: str
    success: bool = False
    diagnosis: str = ""
    steps: list[Step] = field(default_factory=list)
    aftermath: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rntis: dict[str, str] = field(default_factory=dict)
    preamble: int | None = None
    target_cell: int | None = None
    ue_location: dict[str, str] = field(default_factory=dict)
    restarts: dict[str, int] = field(default_factory=dict)
    command: str = ""
    command_answer: str = ""
    ping: str | None = None

    def step(self, key: str) -> Step:
        """The step of this key."""
        return next(step for step in self.steps if step.key == key)

    def to_text(self, *, explain: bool = True) -> str:
        """Human-readable report; with explain, what each step means and the log line that proves it."""
        out = [
            f"Handover of {self.ue} ({self.implementation}): {self.source} -> {self.target}: "
            + ("SUCCESS" if self.success else "FAILED"),
            f"  {self.diagnosis}",
        ]
        if self.command:
            answer = f" -> {self.command_answer}" if self.command_answer else ""
            out.append(f"Command: {self.command}{answer}")
        out.append("Steps ([x] seen in the logs, [ ] missing, [?] not visible in these logs):")
        for number, step in enumerate(self.steps, start=1):
            detail = f": {step.detail}" if step.detail else ""
            out.append(f"  {number}. [{MARKS[step.ok]}] {step.title}{detail}")
            if explain:
                out.append(f"         {step.meaning}")
                if step.evidence:
                    out.append(f"         log: {step.evidence}")
        if self.aftermath:
            out.append("Then:")
            out += [f"  - {event}" for event in self.aftermath]
        if self.restarts:
            out.append("Restarted containers: " + ", ".join(f"{k} (+{v})" for k, v in self.restarts.items()))
        if self.ue_location:
            out.append("UE now:")
            out += [f"  {du}: {line}" for du, line in self.ue_location.items()]
        if self.ping is not None:
            out.append(f"Ping: {self.ping}")
        if self.notes:
            out.append("Notes:")
            out += [f"  - {note}" for note in self.notes]
        return "\n".join(out)


def after_last(lines: list[str], needle: str) -> list[str]:
    """Lines from the last line containing needle on (no line if absent)."""
    for index in range(len(lines) - 1, -1, -1):
        if needle in lines[index]:
            return lines[index:]
    return []


def clean(line: str) -> str:
    """Log line without its color codes and surrounding spaces, to show it as evidence."""
    for code in ("\x1b[0m", "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[93m"):
        line = line.replace(code, "")
    return line.strip()
