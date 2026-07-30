from __future__ import annotations

from copy import deepcopy
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .schemas import PlanRecord


class PlanNotFound(LookupError):
    pass


class PlanStore(Protocol):
    def get(self, *, plan_id: str, user_id: str) -> PlanRecord:
        ...

    def save(self, plan: PlanRecord) -> None:
        ...


def compute_plan_hash(plan: PlanRecord) -> str:
    payload = {
        "analysis_type": plan.analysis_type.value,
        "input_source": plan.input_source.model_dump(mode="json"),
        "requested_params": plan.requested_params,
        "effective_params": plan.effective_params,
        "contrasts": plan.contrasts,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


class InMemoryPlanStore:
    def __init__(self, shared: dict[str, Any] | None = None) -> None:
        self._shared = shared if shared is not None else {}

    def get(self, *, plan_id: str, user_id: str) -> PlanRecord:
        payload = self._shared.get(plan_id)
        if payload is None or payload.get("user_id") != user_id:
            raise PlanNotFound(plan_id)
        return PlanRecord.model_validate(deepcopy(payload))

    def save(self, plan: PlanRecord) -> None:
        self._shared[plan.plan_id] = plan.model_dump(mode="json")


class JsonPlanStore:
    """按 plan_id 原子写入本地运行目录；生产多进程部署可替换为数据库实现。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, *, plan_id: str, user_id: str) -> PlanRecord:
        path = self._path(plan_id)
        if not path.exists():
            raise PlanNotFound(plan_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        plan = PlanRecord.model_validate(payload)
        if plan.user_id != user_id:
            raise PlanNotFound(plan_id)
        return plan

    def save(self, plan: PlanRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(plan.plan_id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        temp.replace(path)

    def _path(self, plan_id: str) -> Path:
        safe = Path(plan_id).name
        if safe != plan_id or not safe:
            raise ValueError("invalid plan_id")
        return self.root / f"{safe}.json"


class PostgresPlanStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def get(self, *, plan_id: str, user_id: str) -> PlanRecord:
        with self._connect() as conn:
            row = conn.execute(
                "select payload from agent_plans where plan_id = %s and user_id = %s",
                (plan_id, user_id),
            ).fetchone()
        if row is None:
            raise PlanNotFound(plan_id)
        return PlanRecord.model_validate(row[0])

    def save(self, plan: PlanRecord) -> None:
        Jsonb = self._jsonb_type()
        payload = plan.model_dump(mode="json")
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into agent_plans (
                    plan_id, run_id, thread_id, user_id, plan_hash, payload,
                    submitted_job_ids, version
                ) values (%s, %s, %s, %s, %s, %s, %s, 0)
                on conflict (plan_id) do update set
                    plan_hash = excluded.plan_hash,
                    payload = excluded.payload,
                    submitted_job_ids = excluded.submitted_job_ids,
                    version = agent_plans.version + 1,
                    updated_at = now()
                where agent_plans.user_id = excluded.user_id
                  and agent_plans.run_id = excluded.run_id
                  and agent_plans.thread_id = excluded.thread_id
                returning plan_id
                """,
                (
                    plan.plan_id,
                    plan.run_id,
                    plan.thread_id,
                    plan.user_id,
                    plan.plan_hash,
                    Jsonb(payload),
                    Jsonb(plan.submitted_job_ids),
                ),
            ).fetchone()
        if row is None:
            raise PlanNotFound(plan.plan_id)

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return psycopg.connect(self.database_url)

    @staticmethod
    def _jsonb_type():
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return Jsonb
