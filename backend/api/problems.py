"""题目与测试用例管理 API。"""
import os
import re

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin
from backend.storage import read_json, atomic_write_json, list_files, locked_update
from backend.utils import now_iso, gen_id, sort_list

problems_bp = Blueprint("problems", __name__)

# ---- 多语言题面 ----
# 默认题面（中文）始终存放在题目顶层字段；其它语言版本存放在
# translations: {<lang>: {title, description, input_description,
#                          output_description, samples, hint}}
# 增删语言版本只动 translations，不触碰中文字段。
DEFAULT_LANG = "zh"
TRANSLATABLE_FIELDS = ("title", "description", "input_description",
                       "output_description", "samples", "hint")
RESERVED_LANGS = {"zh", "cn", "default"}
LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


def _testcases_path(problem_id):
    return os.path.join(config.TESTCASES_DIR, f"{problem_id}.json")


def _problem_path(problem_id):
    return os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")


def load_testcases(problem_id):
    data = read_json(_testcases_path(problem_id))
    return (data or {}).get("cases", []) if data else []


def _non_empty(v):
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def _available_languages(p):
    """题目实际可用的题面语言：默认中文 + 已录入且内容非空的翻译版本。"""
    langs = [DEFAULT_LANG]
    translations = p.get("translations")
    if isinstance(translations, dict):
        for code in sorted(translations):
            tr = translations[code]
            if isinstance(tr, dict) and any(_non_empty(tr.get(f))
                                            for f in TRANSLATABLE_FIELDS):
                langs.append(code)
    return langs


def _problem_summary(p, include_samples=True):
    if not p:
        return None
    # 列表/详情通用摘要：不下发原始 translations，只给可用语言列表
    out = {k: v for k, v in p.items() if k != "translations"}
    out["testcase_count"] = len(load_testcases(p.get("id")))
    if not include_samples:
        out.pop("samples", None)
    out["available_languages"] = _available_languages(p)
    return out


def _localized_problem(p, lang):
    """按语言解析题面：逐字段回退到默认中文，保证不返回空白/残缺内容。

    返回的 dict 附带语言元信息：
      lang            实际采用的语言
      requested_lang  用户请求的语言
      lang_fallback   请求的语言整包缺失，已整体回退中文
      lang_partial    语言版本存在但个别字段缺失，这些字段已用中文补齐
    """
    out = _problem_summary(p, include_samples=True)
    requested = (lang or DEFAULT_LANG).strip().lower() or DEFAULT_LANG
    applied, fallback, partial = DEFAULT_LANG, False, False
    if requested != DEFAULT_LANG:
        translations = p.get("translations")
        tr = translations.get(requested) if isinstance(translations, dict) else None
        if isinstance(tr, dict) and requested in out["available_languages"]:
            applied = requested
            for f in TRANSLATABLE_FIELDS:
                v = tr.get(f)
                if _non_empty(v):
                    out[f] = v
                elif _non_empty(out.get(f)):
                    partial = True
        else:
            fallback = True
    out["lang"] = applied
    out["requested_lang"] = requested
    out["lang_fallback"] = fallback
    out["lang_partial"] = partial
    return out


@problems_bp.get("/problems")
def list_problems():
    keyword = (request.args.get("q") or "").strip().lower()
    tag = request.args.get("tag")
    difficulty = request.args.get("difficulty")
    problems = []
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if not p:
            continue
        if keyword and keyword not in (p.get("title", "") + " " + p.get("description", "")).lower():
            continue
        if tag and tag not in p.get("tags", []):
            continue
        if difficulty:
            try:
                if int(p.get("difficulty", 0)) != int(difficulty):
                    continue
            except (TypeError, ValueError):
                pass
        problems.append(_problem_summary(p, include_samples=False))
    problems = sort_list(problems, key=lambda p: p.get("id", ""), reverse=False)
    return ok({"total": len(problems), "items": problems})


@problems_bp.get("/problems/tags")
def list_tags():
    tags = set()
    for pid in list_files(config.PROBLEMS_DIR):
        p = read_json(os.path.join(config.PROBLEMS_DIR, f"{pid}.json"))
        if p:
            tags.update(p.get("tags", []))
    return ok(sorted(tags))


@problems_bp.get("/problems/<problem_id>")
def get_problem(problem_id):
    p = read_json(_problem_path(problem_id))
    if not p:
        return err("题目不存在", 404)
    lang = request.args.get("lang", DEFAULT_LANG)
    return ok(_localized_problem(p, lang))


@problems_bp.post("/problems")
@require_admin
def create_problem():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return err("题目标题不能为空", 400)
    problem_id = data.get("id") or gen_id("p")
    problem = {
        "id": problem_id,
        "title": title,
        "description": data.get("description", ""),
        "input_description": data.get("input_description", ""),
        "output_description": data.get("output_description", ""),
        "samples": data.get("samples", []),
        "hint": data.get("hint", ""),
        "tags": data.get("tags", []),
        "difficulty": int(data.get("difficulty", 1)),
        "time_limit_ms": int(data.get("time_limit_ms", 1000)),
        "memory_limit_kb": int(data.get("memory_limit_kb", 65536)),
        "languages": data.get("languages", ["python", "cpp", "c", "java"]),
        "points": int(data.get("points", 100)),
        "comparison": data.get("comparison", {"mode": "exact", "float_tolerance": 1e-6,
                                              "ignore_whitespace": True}),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    atomic_write_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"), problem)
    if data.get("cases") is not None:
        _save_testcases(problem_id, data.get("cases", []))
    return ok(_problem_summary(problem))


def _save_testcases(problem_id, cases):
    normalized = []
    for i, c in enumerate(cases):
        normalized.append({
            "id": c.get("id", i + 1),
            "input": c.get("input", ""),
            "output": c.get("output", ""),
            "points": int(c.get("points", 0)),
        })
    atomic_write_json(_testcases_path(problem_id),
                      {"problem_id": problem_id, "cases": normalized})
    # 同步题目中的测试点数量与样例（若未提供样例则取前若干组）
    prob_path = os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")
    p = read_json(prob_path)
    if p:
        if not p.get("samples") and normalized:
            p["samples"] = [{"input": c["input"][:2000], "output": c["output"][:2000]}
                            for c in normalized[:3]]
        p["updated_at"] = now_iso()
        atomic_write_json(prob_path, p)


@problems_bp.put("/problems/<problem_id>")
@require_admin
def update_problem(problem_id):
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
    if not p:
        return err("题目不存在", 404)
    data = request.get_json(silent=True) or {}
    for key in ("title", "description", "input_description", "output_description",
                "hint", "tags", "samples", "languages", "comparison"):
        if key in data:
            p[key] = data[key]
    for key in ("difficulty", "time_limit_ms", "memory_limit_kb", "points"):
        if key in data:
            try:
                p[key] = int(data[key])
            except (TypeError, ValueError):
                pass
    if "title" in data and not (data["title"] or "").strip():
        return err("题目标题不能为空", 400)
    p["updated_at"] = now_iso()
    atomic_write_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"), p)
    if data.get("cases") is not None:
        _save_testcases(problem_id, data.get("cases", []))
    return ok(_problem_summary(p))


@problems_bp.delete("/problems/<problem_id>")
@require_admin
def delete_problem(problem_id):
    p = os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")
    if not os.path.exists(p):
        return err("题目不存在", 404)
    os.remove(p)
    t = _testcases_path(problem_id)
    if os.path.exists(t):
        os.remove(t)
    return ok()


@problems_bp.get("/problems/<problem_id>/testcases")
@require_admin
def get_testcases(problem_id):
    return ok({"problem_id": problem_id, "cases": load_testcases(problem_id)})


@problems_bp.put("/problems/<problem_id>/testcases")
@require_admin
def set_testcases(problem_id):
    data = request.get_json(silent=True) or {}
    _save_testcases(problem_id, data.get("cases", []))
    return ok({"problem_id": problem_id, "count": len(data.get("cases", []))})


# ---- 多语言题面管理（管理员） ----

class _AbortUpdate(Exception):
    """在 locked_update 的 update_fn 中抛出以中止写入并返回错误。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _normalize_translation(data):
    """校验并规范化一个语言版本，返回 (translation, error)。"""
    tr = {}
    for f in ("title", "description", "input_description",
              "output_description", "hint"):
        v = data.get(f)
        if v is None:
            continue
        if not isinstance(v, str):
            return None, f"字段 {f} 必须是字符串"
        tr[f] = v
    if "samples" in data and data["samples"] is not None:
        samples = data["samples"]
        if not isinstance(samples, list):
            return None, "samples 必须是数组"
        norm = []
        for s in samples:
            if not isinstance(s, dict):
                return None, "samples 元素必须是 {input, output} 对象"
            norm.append({"input": str(s.get("input", "")),
                         "output": str(s.get("output", ""))})
        tr["samples"] = norm
    if not any(_non_empty(v) for v in tr.values()):
        return None, "语言版本内容不能为空"
    return tr, None


def _check_lang_code(lang):
    """返回 (code, error_response)；合法时 error_response 为 None。"""
    code = (lang or "").strip().lower()
    if code in RESERVED_LANGS:
        return None, err("中文为默认题面，请直接编辑题目正文", 400)
    if not LANG_CODE_RE.match(code):
        return None, err("语言代码格式不正确（如 en、ja、zh-tw）", 400)
    return code, None


@problems_bp.get("/problems/<problem_id>/translations")
@require_admin
def list_translations(problem_id):
    p = read_json(_problem_path(problem_id))
    if not p:
        return err("题目不存在", 404)
    translations = p.get("translations")
    if not isinstance(translations, dict):
        translations = {}
    return ok({"problem_id": problem_id,
               "translations": translations,
               "available_languages": _available_languages(p)})


@problems_bp.put("/problems/<problem_id>/translations/<lang>")
@require_admin
def upsert_translation(problem_id, lang):
    """新增/覆盖一个语言版本；只写 translations[lang]，不动中文题面。"""
    code, error = _check_lang_code(lang)
    if error:
        return error
    if not os.path.exists(_problem_path(problem_id)):
        return err("题目不存在", 404)
    data = request.get_json(silent=True) or {}
    tr, error = _normalize_translation(data)
    if error:
        return err(error, 400)

    def _update(p):
        if p is None:
            raise _AbortUpdate("题目不存在", 404)
        translations = p.get("translations")
        if not isinstance(translations, dict):
            translations = {}
        translations[code] = tr
        p["translations"] = translations
        p["updated_at"] = now_iso()
        return p

    try:
        updated = locked_update(_problem_path(problem_id), _update)
    except _AbortUpdate as e:
        return err(e.message, e.status)
    return ok(_localized_problem(updated, code))


@problems_bp.delete("/problems/<problem_id>/translations/<lang>")
@require_admin
def delete_translation(problem_id, lang):
    """删除一个语言版本；中文题面与题目其它字段不受影响。"""
    code, error = _check_lang_code(lang)
    if error:
        return error
    if not os.path.exists(_problem_path(problem_id)):
        return err("题目不存在", 404)

    def _update(p):
        if p is None:
            raise _AbortUpdate("题目不存在", 404)
        translations = p.get("translations")
        if not isinstance(translations, dict) or code not in translations:
            raise _AbortUpdate("该语言版本不存在", 404)
        translations.pop(code, None)
        if translations:
            p["translations"] = translations
        else:
            p.pop("translations", None)
        p["updated_at"] = now_iso()
        return p

    try:
        locked_update(_problem_path(problem_id), _update)
    except _AbortUpdate as e:
        return err(e.message, e.status)
    return ok()
