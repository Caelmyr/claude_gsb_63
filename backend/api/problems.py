"""题目与测试用例管理 API。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin
from backend.storage import read_json, atomic_write_json, locked_update, list_files
from backend.utils import now_iso, gen_id, sort_list

problems_bp = Blueprint("problems", __name__)

# ---------------------------------------------------------------------------
# 多语言题面
#
# 约定：题目 JSON 顶层字段（title/description/input_description/
# output_description/samples/hint）始终是「默认中文题面」，任何翻译的增删改
# 都只操作 p["translations"][lang]，绝不触碰顶层中文内容。
# ---------------------------------------------------------------------------

DEFAULT_LANG = "zh"

# code -> (中文名, 英文名)，中文名用于中文界面展示，英文名用于外文界面
SUPPORTED_LANGS = {
    "zh": ("简体中文", "Chinese"),
    "en": ("English", "English"),
    "ja": ("日本語", "Japanese"),
    "ko": ("한국어", "Korean"),
    "fr": ("Français", "French"),
    "ru": ("Русский", "Russian"),
    "de": ("Deutsch", "German"),
    "es": ("Español", "Spanish"),
}

# 可翻译的正文字段（样例单独处理）
TRANSLATABLE_FIELDS = ("title", "description", "input_description",
                       "output_description", "hint")


def _testcases_path(problem_id):
    return os.path.join(config.TESTCASES_DIR, f"{problem_id}.json")


def load_testcases(problem_id):
    data = read_json(_testcases_path(problem_id))
    return (data or {}).get("cases", []) if data else []


def _problem_summary(p, include_samples=True):
    if not p:
        return None
    # translations 只通过专用接口暴露，列表/常规详情不带出，避免列表与搜索
    # 的返回结构受多语言功能影响
    out = {k: v for k, v in p.items() if k != "translations"}
    out["testcase_count"] = len(load_testcases(p.get("id")))
    if not include_samples:
        out.pop("samples", None)
    return out


def _available_langs(p):
    """该题实际已录入的语言列表（中文恒在首位，且去重、过滤脏数据）。"""
    langs = [DEFAULT_LANG]
    translations = p.get("translations")
    if isinstance(translations, dict):
        for code in translations:
            if code != DEFAULT_LANG and code in SUPPORTED_LANGS and code not in langs:
                langs.append(code)
    return langs


def _lang_meta(p, requested):
    return {
        "default_lang": DEFAULT_LANG,
        "requested_lang": requested or DEFAULT_LANG,
        "displayed_lang": DEFAULT_LANG,
        "available_langs": _available_langs(p),
        "supported_langs": [{"code": c, "name": n[0], "en_name": n[1]}
                            for c, n in SUPPORTED_LANGS.items()],
        # 是否发生过回退（整体回退或部分字段回退）
        "fallback": False,
        # 具体哪些字段回退到了中文（含 "samples"）
        "fallback_fields": [],
        # 请求的语言码不被支持
        "invalid_lang": False,
    }


def _localize_problem(p, lang):
    """按指定语言解析题面，返回 (题面副本, lang meta)。

    回退策略（保证页面绝不出现空白或残缺）：
      1. 未带 lang / 语言码非法 / 请求中文：直接返回默认中文；
      2. 该语言整体未录入：整份回退中文，fallback=True；
      3. 已录入但某正文字段为空：仅该字段回退中文并记入 fallback_fields；
      4. 样例按下标对齐：某组翻译样例输入/输出不完整时该组回退中文样例。
    """
    requested = (lang or DEFAULT_LANG).strip()
    out = {k: v for k, v in p.items() if k != "translations"}
    meta = _lang_meta(p, requested)

    if requested not in SUPPORTED_LANGS:
        meta["invalid_lang"] = True
        if requested and requested != DEFAULT_LANG:
            meta["fallback"] = True
        return out, meta
    if requested == DEFAULT_LANG:
        return out, meta

    translations = p.get("translations")
    tr = translations.get(requested) if isinstance(translations, dict) else None
    if not isinstance(tr, dict):
        # 该语言完全未录入：整份回退中文
        meta["fallback"] = True
        return out, meta

    meta["displayed_lang"] = requested
    for field in TRANSLATABLE_FIELDS:
        val = tr.get(field)
        if isinstance(val, str) and val.strip():
            out[field] = val
        else:
            # 顶层中文同名字段缺失时也只保持缺失（与中文页表现一致），
            # 但记录回退以便前端提示
            meta["fallback"] = True
            meta["fallback_fields"].append(field)

    # 样例：按下标与中文样例对齐，翻译不完整的组回退中文组，保证数量与内容完整
    base_samples = out.get("samples") if isinstance(out.get("samples"), list) else []
    tr_samples = tr.get("samples")
    tr_samples = tr_samples if isinstance(tr_samples, list) else []
    merged = []
    samples_fallback = False
    for i, base in enumerate(base_samples):
        cand = tr_samples[i] if i < len(tr_samples) and isinstance(tr_samples[i], dict) else None
        inp = cand.get("input") if cand else None
        outp = cand.get("output") if cand else None
        if isinstance(inp, str) and isinstance(outp, str) and (inp.strip() or outp.strip()):
            merged.append({"input": inp, "output": outp})
        else:
            # 该下标没有完整翻译样例 -> 回退到中文样例
            merged.append({"input": base.get("input", ""), "output": base.get("output", "")})
            samples_fallback = True
    # 声明过翻译样例但存在回退（含组数对不齐），需要提示
    if tr_samples and (samples_fallback or len(tr_samples) != len(base_samples)):
        meta["fallback"] = True
        meta["fallback_fields"].append("samples")
    out["samples"] = merged
    return out, meta


def _normalize_translation(data):
    """清洗一份翻译入参，只保留合法的字符串字段与样例。"""
    if not isinstance(data, dict):
        return {}
    clean = {}
    for field in TRANSLATABLE_FIELDS:
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            clean[field] = val
    samples = []
    raw = data.get("samples")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            inp = item.get("input", "")
            outp = item.get("output", "")
            samples.append({
                "input": inp if isinstance(inp, str) else "",
                "output": outp if isinstance(outp, str) else "",
            })
    # 样例全空就不保存，避免存一份没有意义的空壳
    if any(s["input"].strip() or s["output"].strip() for s in samples):
        clean["samples"] = samples
    return clean


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
        # 列表与搜索始终基于默认中文题面，不受多语言影响
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
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
    if not p:
        return err("题目不存在", 404)
    # 先基于原始数据（含 translations）做语言解析，解析结果本身不含 translations
    localized, meta = _localize_problem(p, request.args.get("lang"))
    localized["testcase_count"] = len(load_testcases(problem_id))
    localized["lang"] = meta
    return ok(localized)


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
        # 翻译版本独立维护，创建时恒为空，不允许通过此接口写入
        "translations": {},
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
    # 注意：白名单不含 translations，通用编辑接口永远无法覆盖/删除翻译版本
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


# ---------------------------------------------------------------------------
# 多语言题面：管理员专用增删改查
# 全部只改 translations 字段，并在单文件锁内读改写，绝不影响顶层中文题面。
# ---------------------------------------------------------------------------

def _problem_path(problem_id):
    return os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json")


def _validate_translation_lang(lang):
    """返回 (lang, error)；中文与非法语言码都不允许通过翻译接口操作。"""
    if lang not in SUPPORTED_LANGS:
        return None, "不支持的语言"
    if lang == DEFAULT_LANG:
        return None, "中文是默认题面，请在题目编辑中维护"
    return lang, None


@problems_bp.get("/problems/<problem_id>/translations")
@require_admin
def list_translations(problem_id):
    p = read_json(_problem_path(problem_id))
    if not p:
        return err("题目不存在", 404)
    translations = p.get("translations")
    translations = translations if isinstance(translations, dict) else {}
    # 返回每种语言的录入情况（含是否为空壳），供管理端判断
    items = []
    for code, name in SUPPORTED_LANGS.items():
        if code == DEFAULT_LANG:
            continue
        tr = translations.get(code)
        items.append({
            "lang": code,
            "name": name[0],
            "en_name": name[1],
            "exists": isinstance(tr, dict) and bool(tr),
        })
    return ok({
        "problem_id": problem_id,
        "default_lang": DEFAULT_LANG,
        "available_langs": _available_langs(p),
        "items": items,
    })


@problems_bp.get("/problems/<problem_id>/translations/<lang>")
@require_admin
def get_translation(problem_id, lang):
    lang, error = _validate_translation_lang(lang)
    if error:
        return err(error, 400)
    p = read_json(_problem_path(problem_id))
    if not p:
        return err("题目不存在", 404)
    translations = p.get("translations")
    tr = translations.get(lang) if isinstance(translations, dict) else None
    # 同时回传默认中文题面，方便管理端对照录入
    base = {field: p.get(field, "") for field in TRANSLATABLE_FIELDS}
    base["samples"] = p.get("samples", []) if isinstance(p.get("samples"), list) else []
    return ok({
        "problem_id": problem_id,
        "lang": lang,
        "exists": isinstance(tr, dict) and bool(tr),
        "translation": tr if isinstance(tr, dict) else {},
        "base": base,
        "available_langs": _available_langs(p),
    })


@problems_bp.put("/problems/<problem_id>/translations/<lang>")
@require_admin
def put_translation(problem_id, lang):
    lang, error = _validate_translation_lang(lang)
    if error:
        return err(error, 400)
    path = _problem_path(problem_id)
    if not os.path.exists(path):
        return err("题目不存在", 404)
    data = request.get_json(silent=True) or {}
    clean = _normalize_translation(data)
    if not clean:
        return err("翻译内容不能为空（至少填写标题、正文或样例之一）", 400)

    def _apply(p):
        if not p:
            raise FileNotFoundError(problem_id)
        translations = p.get("translations")
        # 旧数据损坏（非 dict）时整体重置，避免污染中文题面
        p["translations"] = dict(translations) if isinstance(translations, dict) else {}
        p["translations"][lang] = clean
        p["updated_at"] = now_iso()
        return p

    try:
        p = locked_update(path, _apply)
    except FileNotFoundError:
        return err("题目不存在", 404)
    return ok({"problem_id": problem_id, "lang": lang,
               "available_langs": _available_langs(p)})


@problems_bp.delete("/problems/<problem_id>/translations/<lang>")
@require_admin
def delete_translation(problem_id, lang):
    lang, error = _validate_translation_lang(lang)
    if error:
        return err(error, 400)
    path = _problem_path(problem_id)
    if not os.path.exists(path):
        return err("题目不存在", 404)

    def _apply(p):
        if not p:
            raise FileNotFoundError(problem_id)
        translations = p.get("translations")
        # 只删除目标语言键，顶层中文与其它语言一律不动；键不存在也视为成功
        if isinstance(translations, dict) and lang in translations:
            translations = dict(translations)
            translations.pop(lang, None)
            p["translations"] = translations
            p["updated_at"] = now_iso()
        return p

    try:
        p = locked_update(path, _apply)
    except FileNotFoundError:
        return err("题目不存在", 404)
    return ok({"problem_id": problem_id, "lang": lang,
               "available_langs": _available_langs(p)})
