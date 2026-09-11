"""Bounded in-memory user requests; each turn uses the existing independent Agent run."""

from db_agent.agent import run_agent_observed
from db_agent.config import AnalysisSettings, DatabaseSettings, QuerySettings, Settings
from db_agent.connectors import create_connector
from db_agent.conversation_context import conversation_prompt
from db_agent.presentation import AgentRunResult, has_complete_query_results
from db_agent.records import RunRecord


class ConversationError(RuntimeError):
    """Fixed session diagnostics without user text, configuration or provider errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__({
            "SESSION_BUSY": "当前会话正在运行，请等待本轮清理完成。",
            "SESSION_RESET_REQUIRED": "当前会话需要重置，请使用 /reset 后完整重述请求。",
        }[code])


class ConversationSession:
    """A configuration-bound session retaining only successful raw user requests."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseSettings,
        analysis_settings: AnalysisSettings | None = None,
        query_settings: QuerySettings | None = None,
    ):
        self._settings = settings.model_copy(deep=True)
        self._database = database.model_copy(deep=True)
        self._analysis = (analysis_settings or AnalysisSettings()).model_copy(deep=True)
        self._query = (query_settings or QuerySettings()).model_copy(deep=True)
        self._requests: list[str] = []
        self._needs_reset = False
        self._busy = False

    @property
    def requests(self) -> list[str]:
        return self._requests.copy()

    @property
    def turn_count(self) -> int:
        return len(self._requests)

    @property
    def needs_reset(self) -> bool:
        return self._needs_reset

    def reset(self) -> None:
        if self._busy:
            raise ConversationError("SESSION_BUSY")
        self._requests.clear()
        self._needs_reset = False

    def require_reset(self) -> None:
        """Block continuation after delivery failure without silently clearing context."""
        if self._busy:
            raise ConversationError("SESSION_BUSY")
        self._needs_reset = True

    async def submit(self, prompt: str, record: RunRecord | None = None) -> AgentRunResult:
        if self._busy:
            raise ConversationError("SESSION_BUSY")
        if self._needs_reset:
            raise ConversationError("SESSION_RESET_REQUIRED")
        self._busy = True
        try:
            previous = self._requests.copy()
            # Validate limits before creating a connector or calling the Agent.
            # The Agent formats this same context for all of its model phases.
            conversation_prompt(previous, prompt)
            result = await run_agent_observed(
                prompt, self._settings.model_copy(deep=True),
                create_connector(self._database.model_copy(deep=True)), record,
                self._analysis.model_copy(deep=True), self._query.model_copy(deep=True),
                previous_requests=previous,
            )
            if not isinstance(result, AgentRunResult):
                raise RuntimeError("Agent 返回格式无效。")
            if has_complete_query_results(result):
                self._requests.append(prompt)
            else:
                self._needs_reset = True
            return result
        except BaseException:
            self._needs_reset = True
            raise
        finally:
            self._busy = False
