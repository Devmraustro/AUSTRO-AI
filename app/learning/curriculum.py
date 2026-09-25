"""AUSTRO AI - Curriculum engine (Phase E).

Builds an editable, prerequisite-respecting curriculum from learning
objectives. Modules are created by grouping a topological (dependency-safe)
ordering; every operation is deterministic and owner-scoped.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.learning.models import CURRICULUM_MODES, Curriculum, CurriculumModule
from app.learning.repositories import LearningStore

logger = logging.getLogger(__name__)

_MODULE_SIZE = 4


def topological_order(objectives: List[Dict[str, Any]]) -> List[int]:
    """Return objective_ids in dependency-safe order (Kahn's algorithm).

    Objectives whose prerequisites reference unknown ids are treated as having
    no prerequisites. Cycle members are emitted last, in id order.
    """
    by_id = {int(o["objective_id"]): o for o in objectives}
    known = set(by_id)
    resolved: List[int] = []
    remaining = set(known)

    def satisfied(obj_id: int) -> bool:
        prereqs = [int(p) for p in (by_id[obj_id].get("prerequisites") or [])
                   if int(p) in known]
        return all(p in set(resolved) for p in prereqs)

    changed = True
    while changed:
        changed = False
        for obj_id in sorted(remaining):
            if satisfied(obj_id):
                remaining.discard(obj_id)
                resolved.append(obj_id)
                changed = True
                break
    resolved.extend(sorted(remaining))
    return resolved


def group_modules(ordered: List[int], objectives: List[Dict[str, Any]],
                  mode: str, size: Optional[int] = None) -> List[Dict[str, Any]]:
    """Split the ordered objective ids into titled curriculum modules."""
    if mode not in CURRICULUM_MODES:
        mode = "READ"
    per_module = size or (_MODULE_SIZE if mode != "FAST" else 3)
    titles = {int(o["objective_id"]): (o.get("title") or "") for o in objectives}
    modules: List[Dict[str, Any]] = []
    for index in range(0, len(ordered), per_module):
        slice_ids = ordered[index:index + per_module]
        first = titles.get(slice_ids[0], "")
        module_no = index // per_module + 1
        modules.append({
            "module_index": module_no,
            "title": f"الوحدة {module_no}: {first}" if first else f"الوحدة {module_no}",
            "objective_ids": slice_ids,
        })
    if not modules:
        modules.append({"module_index": 1, "title": "الوحدة 1",
                        "objective_ids": []})
    return modules


class CurriculumEngine:
    def __init__(self, store: LearningStore):
        self._store = store

    # -- build --------------------------------------------------------------
    def build(self, *, owner_user_id: int, goal_id: int, title: str,
              mode: str = "READ",
              objective_ids: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
        objectives = self._store.objectives.list_for_goal(goal_id)
        if objective_ids is not None:
            allowed = set(objective_ids)
            objectives = [o for o in objectives if o["objective_id"] in allowed]
        ordered = topological_order(objectives)
        modules = group_modules(ordered, objectives, mode)
        curriculum_id = self._store.curricula.create(
            owner_user_id=owner_user_id, goal_id=goal_id, title=title,
            mode=mode, modules=modules,
        )
        if curriculum_id is None:
            return None
        return self._store.curricula.get(owner_user_id, curriculum_id)

    # -- reads --------------------------------------------------------------
    def get(self, owner_user_id: int, curriculum_id: int) -> Optional[Dict[str, Any]]:
        return self._store.curricula.get(owner_user_id, curriculum_id)

    def for_goal(self, owner_user_id: int, goal_id: int) -> List[Dict[str, Any]]:
        return self._store.curricula.list_for_goal(owner_user_id, goal_id)

    def ordered_objective_ids(self, curriculum: Dict[str, Any]) -> List[int]:
        modules = curriculum.get("modules") or []
        out: List[int] = []
        seen = set()
        for module in sorted(modules, key=lambda m: m.get("module_index", 0)):
            for obj_id in module.get("objective_ids") or []:
                if obj_id not in seen:
                    seen.add(obj_id)
                    out.append(int(obj_id))
        return out

    # -- editing (curriculum is user-editable) ------------------------------
    def set_objectives(self, owner_user_id: int, curriculum_id: int,
                       objective_ids: List[int]) -> bool:
        curriculum = self._store.curricula.get(owner_user_id, curriculum_id)
        if curriculum is None:
            return False
        goal_id = curriculum["goal_id"]
        objectives = self._store.objectives.list_for_goal(goal_id)
        index = {int(o["objective_id"]): o for o in objectives}
        ordered = [oid for oid in objective_ids if oid in index] + \
            [oid for oid in topological_order(objectives) if oid not in set(objective_ids)]
        modules = group_modules(ordered, objectives, curriculum.get("mode") or "READ")
        return self._store.curricula.update_modules(
            owner_user_id, curriculum_id, modules,
        )

    def reorder(self, owner_user_id: int, curriculum_id: int,
                new_order: List[int]) -> bool:
        curriculum = self._store.curricula.get(owner_user_id, curriculum_id)
        if curriculum is None:
            return False
        current = self.ordered_objective_ids(curriculum)
        current_set = set(current)
        commands = [
            new_order if set(new_order) == current_set and len(new_order) == len(current)
            else (new_order + [oid for oid in current if oid not in set(new_order)])
        ][0]
        return self.set_objectives(owner_user_id, curriculum_id, commands)

    def rename_module(self, owner_user_id: int, curriculum_id: int,
                      module_index: int, title: str) -> bool:
        curriculum = self._store.curricula.get(owner_user_id, curriculum_id)
        if curriculum is None:
            return False
        modules = curriculum.get("modules") or []
        for module in modules:
            if module.get("module_index") == module_index:
                module["title"] = title
        return self._store.curricula.update_modules(
            owner_user_id, curriculum_id, modules,
        )

    def to_model(self, curriculum: Dict[str, Any]) -> Curriculum:
        modules = [
            CurriculumModule(module_index=m.get("module_index", i + 1),
                             title=m.get("title", ""),
                             objective_ids=[int(x) for x in (m.get("objective_ids") or [])])
            for i, m in enumerate(curriculum.get("modules") or [])
        ]
        return Curriculum(curriculum_id=curriculum["curriculum_id"],
                          owner_user_id=curriculum["owner_user_id"],
                          goal_id=curriculum["goal_id"],
                          title=curriculum.get("title", ""),
                          mode=curriculum.get("mode", "READ"),
                          modules=modules)


__all__ = ["CurriculumEngine", "topological_order", "group_modules"]