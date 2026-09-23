from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from models import FT8Message, MessageType, QSOPhase


DEFAULT_TRIALS = 10


class QSOStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"


@dataclass
class QSOStage:
    phase: QSOPhase

    response_text: Optional[str] = None
    remaining_trials: int = 0

    rx_count: int = 0
    tx_count: int = 0

    entered_utc: Optional[int] = None
    last_rx_utc: Optional[int] = None
    last_tx_utc: Optional[int] = None

    last_rx_message_id: Optional[int] = None


@dataclass
class RuntimeQSO:
    qso_id: int
    callsign: str
    grid: Optional[str] = None
    signal: Optional[int] = None

    local_call: str = "AC8SS"
    max_trials: int = DEFAULT_TRIALS

    stages: list[QSOStage] = field(
        default_factory=lambda: [
            QSOStage(QSOPhase.REPLIED),
            QSOStage(QSOPhase.CONFIRMED),
            QSOStage(QSOPhase.COMPLETED),
        ]
    )

    current_stage_index: int = 0
    status: QSOStatus = QSOStatus.ACTIVE

    @property
    def current_stage(self) -> QSOStage:
        return self.stages[self.current_stage_index]

    @property
    def phase(self) -> QSOPhase:
        return self.current_stage.phase

    def _activate_stage(
        self,
        phase: QSOPhase,
        *,
        utc: int,
        response_text: Optional[str],
    ) -> None:
        index = next(
            i for i, stage in enumerate(self.stages)
            if stage.phase == phase
        )

        self.current_stage_index = index
        stage = self.current_stage

        if stage.entered_utc is None:
            stage.entered_utc = utc

        stage.response_text = response_text
        stage.remaining_trials = (
            self.max_trials if response_text is not None else 0
        )

        if phase == QSOPhase.COMPLETED:
            self.status = QSOStatus.COMPLETED
        else:
            self.status = QSOStatus.ACTIVE

    def _report_response(self) -> Optional[str]:
        if self.signal is None:
            return None

        return (
            f"{self.callsign} "
            f"{self.local_call} "
            f"{self.signal:+03d}"
        )

    def _rr73_response(self) -> str:
        return f"{self.callsign} {self.local_call} RR73"

    def update_rx(self, msg: FT8Message) -> None:
        """
        Consume an incoming message belonging to this QSO.

        Responsibilities:
        - update RX/liveness information
        - revive an expired QSO
        - advance protocol phase when appropriate
        - construct the response for the current phase
        - refresh remaining_trials
        """

        if msg.sender != self.callsign:
            raise ValueError(
                f"RX sender {msg.sender!r} does not match "
                f"QSO callsign {self.callsign!r}"
            )

        #
        # A relevant RX proves the remote station is alive.
        # EXPIRED is therefore reversible.
        #
        if self.status == QSOStatus.EXPIRED:
            self.status = QSOStatus.ACTIVE

        stage = self.current_stage
        stage.rx_count += 1
        stage.last_rx_utc = msg.utc
        stage.last_rx_message_id = msg.message_id

        #
        # COMPLETED is terminal.
        #
        if self.status == QSOStatus.COMPLETED:
            return

        #
        # Stage 1: REPLIED
        #
        if self.phase == QSOPhase.REPLIED:
            if msg.type in (
                MessageType.R_REPORT,
                MessageType.RRR,
            ):
                self._activate_stage(
                    QSOPhase.CONFIRMED,
                    utc=msg.utc,
                    response_text=self._rr73_response(),
                )
                return

            if msg.type in (
                MessageType.RR73,
                MessageType.MSG_73,
            ):
                self._activate_stage(
                    QSOPhase.COMPLETED,
                    utc=msg.utc,
                    response_text=None,
                )
                return

            # GRID / REPORT / repeated old-stage traffic:
            # stay in REPLIED and restart the full TX budget.
            stage.response_text = self._report_response()
            stage.remaining_trials = self.max_trials
            return

        #
        # Stage 2: CONFIRMED
        #
        if self.phase == QSOPhase.CONFIRMED:
            if msg.type in (
                MessageType.RR73,
                MessageType.MSG_73,
            ):
                self._activate_stage(
                    QSOPhase.COMPLETED,
                    utc=msg.utc,
                    response_text=None,
                )
                return

            # Any repeated earlier-stage response means the remote
            # station is still waiting for our RR73.
            stage.response_text = self._rr73_response()
            stage.remaining_trials = self.max_trials
            return

    def record_tx(self) -> None:
        """
        Called after the current response was actually transmitted.
        """
        if self.status != QSOStatus.ACTIVE:
            return

        stage = self.current_stage

        if stage.response_text is None:
            return

        if stage.remaining_trials <= 0:
            return

        stage.tx_count += 1
        stage.remaining_trials -= 1

    def expire(self) -> None:
        """
        Temporarily stop scheduling this QSO.

        A later relevant RX can reactivate it.
        """
        if self.status != QSOStatus.COMPLETED:
            self.status = QSOStatus.EXPIRED

    def can_transmit(self) -> bool:
        return (
            self.status == QSOStatus.ACTIVE
            and self.current_stage.response_text is not None
            and self.current_stage.remaining_trials > 0
        )

    def next_response(self) -> Optional[str]:
        if not self.can_transmit():
            return None

        return self.current_stage.response_text
