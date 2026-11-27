"""Hard rules enforced in code (PRD §6.6, §6.7). The prompt asks the model to behave; this makes sure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COMPLAINT_HOLDING_MESSAGE = (
    "Thank you for letting us know, and I'm sorry to hear that. I've passed this directly to the owner, who will "
    "personally follow up with you as soon as possible."
)
DNC_CLOSING_MESSAGE = "Understood, we won't message you again. Have a great day!"
MAX_DM_CHARS = 900
MAX_SENDS_PER_TURN = 2

PROMISE_RE = re.compile(
    r"\b(?:guarantee[ds]?|perfect(?:ly)?|flawless(?:ly)?|like new|brand new|completely remove[sd]?|"
    r"all (?:the )?stains|every stain|good as new|showroom)\b|\b100\s?%",
    re.IGNORECASE,
)
CONFIRM_RE = re.compile(r"\b(you'?re all set|confirmed|you'?re booked|is booked|booked you)\b", re.IGNORECASE)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")
HARD_OPT_OUT = {"stop", "unsubscribe", "stop messaging me", "remove me", "not interested", "stop texting me",
                "leave me alone"}


class GuardrailBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class TurnState:
    verified_amounts: set[int] = field(default_factory=set)
    booking_succeeded: bool = False
    approved_confirmation: bool = False
    complaint_mode: bool = False
    sends: int = 0
    shadow: bool = False


def is_hard_opt_out(text: str) -> bool:
    t = re.sub(r"[^\w\s']", "", text.strip().lower())
    return t in HARD_OPT_OUT


def check_outgoing(text: str, state: TurnState) -> None:
    """Raises GuardrailBlocked with a reason the model can act on. Window/DNC/pause are checked by the sender."""
    if state.sends >= MAX_SENDS_PER_TURN:
        raise GuardrailBlocked("Already sent 2 messages this turn. End your turn.")
    if state.complaint_mode and text.strip() != COMPLAINT_HOLDING_MESSAGE:
        raise GuardrailBlocked("Complaint handling: send exactly the holding message from AGENT_INSTRUCTIONS §4.4.")
    if len(text) > MAX_DM_CHARS:
        raise GuardrailBlocked(f"Message is {len(text)} characters; the limit is {MAX_DM_CHARS}. Shorten it.")
    if m := PROMISE_RE.search(text):
        raise GuardrailBlocked(f"Promise language not allowed ('{m.group(0)}'). Use honest wording instead.")
    for raw in PRICE_RE.findall(text):
        amount = float(raw.replace(",", ""))
        if amount != int(amount) or int(amount) not in state.verified_amounts:
            raise GuardrailBlocked(f"Unverified price ${raw}. Only state amounts returned by quote_price, "
                                   "book_appointment or an approved override.")
    if CONFIRM_RE.search(text) and not (state.booking_succeeded or state.approved_confirmation):
        raise GuardrailBlocked("Don't say a booking is confirmed unless book_appointment succeeded this turn.")
