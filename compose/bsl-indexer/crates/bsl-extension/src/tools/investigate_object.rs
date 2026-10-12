// MCP-tool `investigate_object` — досье объекта метаданных 1С за ОДИН вызов:
// паспорт + сжатая структура + формы/модули (перечни) + карта влияния (сэмплы) +
// точки входа в модулях объекта (имена процедур без тел).
//
// Зачем отдельный tool: горячий сценарий «разбери объект / что затронет правка»
// раньше собирался серией get_object_structure + get_object_profile +
// find_references + get_file_summary — 4–8 round-trip'ов. Здесь один компактный
// агрегат + next_hint с конкретными следующими вызовами.
//
// Не заменяет точечные tools: тела процедур — get_function; полная карта
// влияния — find_references; полный паспорт с expand — get_object_profile.

use std::future::Future;
use std::pin::Pin;

use code_index_core::extension::{IndexTool, ToolContext};
use rusqlite::params;
use serde_json::{json, Map, Value};

const REPO: &str = "default";
const ATTR_SAMPLE: usize = 15;
const PROC_CAP: usize = 20;
const IMPACT_SAMPLE: i64 = 8;
const DEEP_BODIES: usize = 3;
const QUERY_CAP: usize = 30;
const QUERY_TIMEOUT_SECS: u64 = 8;

/// Имена обработчиков, которые поднимаем в начало списка точек входа.
const ENTRY_PRIORITY: &[&str] = &[
    "ОбработкаПроведения",
    "ОбработкаУдаленияПроведения",
    "ПередЗаписью",
    "ПриЗаписи",
    "ПередУдалением",
    "ОбработкаЗаполнения",
    "ОбработкаПроверкиЗаполнения",
    "ПриСозданииНаСервере",
    "ПриОткрытии",
];

pub struct InvestigateObjectTool;

impl IndexTool for InvestigateObjectTool {
    fn name(&self) -> &str {
        "investigate_object"
    }

    fn description(&self) -> &str {
        "Досье объекта метаданных 1С за ОДИН вызов — стартовая точка анализа \
         ('Document.РеализацияТоваровУслуг', 'Catalog.Контрагенты'): паспорт \
         (full_name/meta_type/name/synonym), сжатая structure (счётчики + образец \
         реквизитов), forms/modules перечнем, impact (сжатый find_references: \
         data_refs/code_usages/role_rights с total+sample), entry_procedures \
         (точки входа модулей объекта — имена/пути/строки БЕЗ тел). \
         Вход: full_name ИЛИ query (подстрока имени, как name_like; при многих \
         совпадениях — только паспорта кандидатов + next_hint). \
         depth='overview' (дефолт) | 'deep' (до 3 тел ключевых entry_procedures). \
         НЕ заменяет get_function (тело), get_form_handlers, find_references \
         (полная карта), get_object_profile(expand=…). For BSL/1C only."
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Алиас репозитория (из --path alias=dir или daemon.toml)"
                },
                "full_name": {
                    "type": "string",
                    "description": "Полное имя объекта '<MetaType>.<Name>', например 'Catalog.Контрагенты'. Если известно — предпочтительнее query."
                },
                "query": {
                    "type": "string",
                    "description": "Подстрока имени объекта (без префикса типа), как name_like у get_object_structure. Один матч → полное досье; несколько → список паспортов без структуры."
                },
                "meta_type": {
                    "type": "string",
                    "description": "Необязательный фильтр типа для query: 'Catalog'/'Document'/… (RU тоже). Без query не действует."
                },
                "depth": {
                    "type": "string",
                    "enum": ["overview", "deep"],
                    "description": "overview (дефолт) — без тел процедур; deep — добавить до 3 тел из entry_procedures (с обычным cap тела)."
                },
                "max_response_bytes": {
                    "type": "integer",
                    "description": "Бюджет размера ЭТОГО ответа в байтах — перекрывает серверный на один вызов. Запрос сверх потолка зажимается; фактически применённое — response_budget_applied."
                }
            },
            "required": ["repo"]
        })
    }

    fn applicable_languages(&self) -> Option<&'static [&'static str]> {
        Some(&["bsl"])
    }

    fn execute<'a>(
        &'a self,
        args: Value,
        ctx: ToolContext<'a>,
    ) -> Pin<Box<dyn Future<Output = Value> + Send + 'a>> {
        Box::pin(async move {
            let depth = args
                .get("depth")
                .and_then(|v| v.as_str())
                .unwrap_or("overview");
            let deep = depth == "deep";
            let budget = code_index_core::mcp::cap::resolve_request_budget(
                args.get("max_response_bytes")
                    .and_then(|v| v.as_u64())
                    .map(|n| n as usize),
            );

            let storage = match ctx.storage.get().await {
                Ok(s) => s,
                Err(e) => {
                    return crate::tools::wrap_error(json!({
                        "error": format!("storage pool: {}", e)
                    }));
                }
            };
            let conn = storage.conn();

            let full_name_arg = args
                .get("full_name")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty());
            let query = args
                .get("query")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty());
            let meta_type = args.get("meta_type").and_then(|v| v.as_str());

            let target = match (full_name_arg, query) {
                (Some(fn_), _) => Some(crate::code_usages::normalize_object_ref(fn_).into_owned()),
                (None, Some(q)) => {
                    match crate::tools::expand_object_criterion(conn, q, meta_type, QUERY_CAP) {
                        Ok((names, truncated)) if names.is_empty() => {
                            return crate::tools::wrap_error(json!({
                                "error": format!("по query='{}' объекты не найдены", q),
                                "hint": "Уточните подстроку или meta_type; либо передайте полный full_name.",
                            }));
                        }
                        Ok((names, truncated)) if names.len() > 1 || truncated => {
                            return candidates_response(
                                conn,
                                &names,
                                truncated,
                                ctx.repo,
                                q,
                                budget.applied,
                            );
                        }
                        Ok((names, _)) => Some(names.into_iter().next().unwrap()),
                        Err(e) => {
                            return crate::tools::wrap_error(json!({
                                "error": format!("database error (query): {}", e)
                            }));
                        }
                    }
                }
                (None, None) => {
                    return crate::tools::wrap_error(json!({
                        "error": "укажите full_name или query",
                        "hint": "full_name='Catalog.X' либо query='Соглашения' (+ опц. meta_type)."
                    }));
                }
            };

            let Some(full_name_raw) = target else {
                return crate::tools::wrap_error(json!({"error": "internal: empty target"}));
            };
            let full_name = crate::tools::canonical_object_name(conn, &full_name_raw);

            let handle = conn.get_interrupt_handle();
            let timer = tokio::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(QUERY_TIMEOUT_SECS)).await;
                handle.interrupt();
            });

            let dossier = assemble_dossier(conn, &full_name, deep, ctx.repo);
            timer.abort();

            let value = match dossier {
                Ok(v) => v,
                Err(e) => {
                    return crate::tools::wrap_error(json!({
                        "error": format!("database error: {}", e)
                    }));
                }
            };

            // Досье — структурный ответ: посекционный omit, не слепой cap массивов.
            let (value, omitted) =
                code_index_core::mcp::cap::omit_oversize_sections(value, budget.applied);
            let mut out = crate::tools::wrap_with_meta_structural(value, Vec::new(), omitted);
            if let Some(obj) = out.as_object_mut() {
                if budget.requested.is_some() || omitted {
                    obj.insert("response_budget_applied".into(), json!(budget.applied));
                }
            }
            out
        })
    }
}

fn candidates_response(
    conn: &rusqlite::Connection,
    names: &[String],
    truncated: bool,
    repo: &str,
    query: &str,
    budget: usize,
) -> Value {
    let mut items = Vec::new();
    for fn_ in names {
        let canon = crate::tools::canonical_object_name(conn, fn_);
        match conn.query_row(
            "SELECT meta_type, name, synonym FROM metadata_objects \
             WHERE repo = ?1 AND full_name = ?2",
            params![REPO, &canon],
            |r| {
                Ok(json!({
                    "full_name": canon,
                    "meta_type": r.get::<_, String>(0)?,
                    "name": r.get::<_, String>(1)?,
                    "synonym": r.get::<_, Option<String>>(2)?,
                }))
            },
        ) {
            Ok(v) => items.push(v),
            Err(_) => items.push(json!({ "full_name": canon })),
        }
    }
    let mut result = json!({
        "matched": items.len(),
        "truncated": truncated,
        "query": query,
        "candidates": items,
        "next_hint": format!(
            "Несколько объектов по query='{}'. Выберите один и вызовите \
             investigate_object(repo='{}', full_name='…') — можно несколько \
             параллельно. Либо сузьте query / meta_type.",
            query, repo
        ),
    });
    let (result, omitted) =
        code_index_core::mcp::cap::omit_oversize_sections(result, budget);
    let mut out = crate::tools::wrap_with_meta_structural(result, Vec::new(), omitted);
    if let Some(obj) = out.as_object_mut() {
        obj.insert("response_budget_applied".into(), json!(budget));
    }
    out
}

fn assemble_dossier(
    conn: &rusqlite::Connection,
    full_name: &str,
    deep: bool,
    repo_alias: &str,
) -> rusqlite::Result<Value> {
    let (meta_type, name) = match full_name.split_once('.') {
        Some((mt, nm)) => (mt.to_string(), nm.to_string()),
        None => {
            return Ok(json!({
                "error": format!("full_name '{}' must be '<MetaType>.<Name>'", full_name)
            }));
        }
    };

    let header = conn.query_row(
        "SELECT meta_type, name, synonym, attributes_json \
         FROM metadata_objects WHERE repo = ?1 AND full_name = ?2",
        params![REPO, full_name],
        |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
            ))
        },
    );

    let (found, db_meta_type, db_name, synonym, structure_raw) = match header {
        Ok((mt, nm, syn, attrs)) => {
            let structure = attrs
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .unwrap_or(Value::Null);
            (true, mt, nm, syn, structure)
        }
        Err(rusqlite::Error::QueryReturnedNoRows) => {
            (false, meta_type.clone(), name.clone(), None, Value::Null)
        }
        Err(e) => return Err(e),
    };

    let structure_summary = summarize_structure(&structure_raw);
    let folder = crate::tools::meta_type_to_folder(&db_meta_type);
    let owner_plural = folder
        .as_deref()
        .map(|fld| format!("{}.{}", fld, db_name));
    let module_prefix = folder
        .as_deref()
        .map(|fld| format!("{}.{}.", fld, db_name));

    let forms = if let Some(ref owner) = owner_plural {
        query_forms_light(conn, owner)?
    } else {
        Vec::new()
    };
    let modules = if let Some(ref pref) = module_prefix {
        query_modules_light(conn, pref)?
    } else {
        Vec::new()
    };

    let object_key = full_name.to_lowercase();
    let impact = query_impact_light(conn, &object_key)?;

    let code_paths: Vec<String> = modules
        .iter()
        .filter_map(|m| m.get("code_path").and_then(|v| v.as_str()).map(str::to_string))
        .filter(|p| !p.is_empty())
        .collect();

    let (entry_procedures, deep_bodies) =
        query_entry_procedures(conn, &code_paths, deep)?;

    let top_form = forms
        .iter()
        .max_by_key(|f| f["handlers_count"].as_u64().unwrap_or(0))
        .and_then(|f| f["form_name"].as_str())
        .unwrap_or("")
        .to_string();
    let top_proc = entry_procedures
        .first()
        .and_then(|p| p.get("name").and_then(|v| v.as_str()))
        .unwrap_or("")
        .to_string();

    let next_hint = build_next_hint(repo_alias, full_name, &top_form, &top_proc, deep);

    let mut obj = Map::new();
    obj.insert("full_name".into(), json!(full_name));
    obj.insert("found".into(), json!(found));
    obj.insert("meta_type".into(), json!(db_meta_type));
    obj.insert("name".into(), json!(db_name));
    obj.insert("synonym".into(), json!(synonym));
    obj.insert("structure".into(), structure_summary);
    obj.insert("forms".into(), json!(forms));
    obj.insert("forms_total".into(), json!(forms.len()));
    obj.insert("modules".into(), json!(modules));
    obj.insert("modules_total".into(), json!(modules.len()));
    obj.insert("impact".into(), impact);
    obj.insert("entry_procedures".into(), json!(entry_procedures));
    obj.insert(
        "entry_procedures_total".into(),
        json!(entry_procedures.len()),
    );
    if deep {
        obj.insert("deep_bodies".into(), json!(deep_bodies));
        obj.insert("depth".into(), json!("deep"));
    } else {
        obj.insert("depth".into(), json!("overview"));
    }
    obj.insert("next_hint".into(), json!(next_hint));
    Ok(Value::Object(obj))
}

fn summarize_structure(structure: &Value) -> Value {
    if structure.is_null() {
        return json!({ "available": false });
    }
    let sample_arr = |key: &str| -> (usize, Vec<Value>) {
        let arr = structure.get(key).and_then(|v| v.as_array());
        let total = arr.map(|a| a.len()).unwrap_or(0);
        let sample: Vec<Value> = arr
            .map(|a| {
                a.iter()
                    .take(ATTR_SAMPLE)
                    .map(|item| {
                        // Оставляем опознание реквизита, без раздувания типов.
                        if let Some(obj) = item.as_object() {
                            let mut m = Map::new();
                            for k in ["name", "synonym", "type", "types", "required"] {
                                if let Some(v) = obj.get(k) {
                                    m.insert(k.into(), v.clone());
                                }
                            }
                            if m.is_empty() {
                                item.clone()
                            } else {
                                Value::Object(m)
                            }
                        } else {
                            item.clone()
                        }
                    })
                    .collect()
            })
            .unwrap_or_default();
        (total, sample)
    };

    let (attr_total, attr_sample) = sample_arr("attributes");
    let (tab_total, _) = sample_arr("tabular_sections");
    let (dim_total, _) = sample_arr("dimensions");
    let (res_total, _) = sample_arr("resources");
    let (enum_total, _) = sample_arr("enum_values");

    json!({
        "available": true,
        "attributes_total": attr_total,
        "attributes_sample": attr_sample,
        "tabular_sections_total": tab_total,
        "dimensions_total": dim_total,
        "resources_total": res_total,
        "enum_values_total": enum_total,
        "hint": "Полные секции — get_object_structure(full_name=…, sections=[…]) \
                 или get_object_profile(sections=['structure'])."
    })
}

fn query_forms_light(
    conn: &rusqlite::Connection,
    owner_full_name: &str,
) -> rusqlite::Result<Vec<Value>> {
    let mut stmt = conn.prepare(
        "SELECT form_name, handlers_json FROM metadata_forms \
         WHERE repo = ?1 AND owner_full_name = ?2 ORDER BY form_name",
    )?;
    let rows = stmt.query_map(params![REPO, owner_full_name], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })?;
    let mut out = Vec::new();
    for row in rows {
        let (form_name, handlers_json) = row?;
        let count = handlers_json
            .as_deref()
            .and_then(|s| serde_json::from_str::<Value>(s).ok())
            .and_then(|v| v.as_array().map(|a| a.len()))
            .unwrap_or(0);
        out.push(json!({ "form_name": form_name, "handlers_count": count }));
    }
    Ok(out)
}

fn query_modules_light(
    conn: &rusqlite::Connection,
    full_name_prefix: &str,
) -> rusqlite::Result<Vec<Value>> {
    let like = format!(
        "{}%",
        full_name_prefix.replace('%', "\\%").replace('_', "\\_")
    );
    let mut stmt = conn.prepare(
        "SELECT module_type, code_path, extension_name \
         FROM metadata_modules WHERE repo = ?1 AND full_name LIKE ?2 ESCAPE '\\' \
         ORDER BY extension_name, module_type",
    )?;
    let rows = stmt.query_map(params![REPO, like], |r| {
        Ok(json!({
            "module_type": r.get::<_, String>(0)?,
            "code_path": r.get::<_, Option<String>>(1)?,
            "extension_name": r.get::<_, Option<String>>(2)?,
        }))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

fn query_impact_light(conn: &rusqlite::Connection, object_key: &str) -> rusqlite::Result<Value> {
    let count = |sql: &str| -> rusqlite::Result<i64> {
        conn.query_row(sql, params![REPO, object_key], |r| r.get(0))
    };
    let data_total = count(
        "SELECT COUNT(*) FROM data_links WHERE repo = ?1 AND to_object_key = ?2",
    )
    .unwrap_or(0);
    let code_total = count(
        "SELECT COUNT(*) FROM metadata_code_usages WHERE repo = ?1 AND object_ref_key = ?2",
    )
    .unwrap_or(0);
    let roles_total = count(
        "SELECT COUNT(*) FROM role_rights WHERE repo = ?1 AND object_name_key = ?2",
    )
    .unwrap_or(0);

    let mut data_sample = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT from_object, from_path, link_kind FROM data_links \
             WHERE repo = ?1 AND to_object_key = ?2 \
             ORDER BY link_kind, from_object LIMIT ?3",
        )?;
        let rows = stmt.query_map(params![REPO, object_key, IMPACT_SAMPLE], |r| {
            Ok(json!({
                "from_object": r.get::<_, String>(0)?,
                "from_path": r.get::<_, String>(1)?,
                "link_kind": r.get::<_, String>(2)?,
            }))
        })?;
        for row in rows {
            data_sample.push(row?);
        }
    }
    let mut code_sample = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT file_path, line, usage_kind FROM metadata_code_usages \
             WHERE repo = ?1 AND object_ref_key = ?2 \
             ORDER BY usage_kind, file_path, line LIMIT ?3",
        )?;
        let rows = stmt.query_map(params![REPO, object_key, IMPACT_SAMPLE], |r| {
            Ok(json!({
                "file_path": r.get::<_, String>(0)?,
                "line": r.get::<_, i64>(1)?,
                "usage_kind": r.get::<_, String>(2)?,
            }))
        })?;
        for row in rows {
            code_sample.push(row?);
        }
    }
    let mut roles_sample = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT role_name, right_name FROM role_rights \
             WHERE repo = ?1 AND object_name_key = ?2 \
             ORDER BY role_name, right_name LIMIT ?3",
        )?;
        let rows = stmt.query_map(params![REPO, object_key, IMPACT_SAMPLE], |r| {
            Ok(json!({
                "role_name": r.get::<_, String>(0)?,
                "right_name": r.get::<_, String>(1)?,
            }))
        })?;
        for row in rows {
            roles_sample.push(row?);
        }
    }

    Ok(json!({
        "data_refs": { "total": data_total, "sample": data_sample },
        "code_usages": { "total": code_total, "sample": code_sample },
        "role_rights": { "total": roles_total, "sample": roles_sample },
        "hint": "Полная карта влияния — find_references(object=…); \
                 одна секция страницами — find_references(section=…)."
    }))
}

fn query_entry_procedures(
    conn: &rusqlite::Connection,
    code_paths: &[String],
    deep: bool,
) -> rusqlite::Result<(Vec<Value>, Vec<Value>)> {
    if code_paths.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }

    // Собираем процедуры по путям модулей объекта. Приоритет — стандартные
    // обработчики объекта/формы, затем остальные по порядку в файле.
    // Динамический OR fl.path = ?N — список путей модулей объекта обычно короткий.
    let mut sql = String::from(
        "SELECT f.name, fl.path, f.line_start, f.line_end, f.args, f.return_type, f.body \
         FROM functions f JOIN files fl ON fl.id = f.file_id WHERE ",
    );
    let mut path_params: Vec<String> = Vec::new();
    for (i, p) in code_paths.iter().enumerate() {
        if i > 0 {
            sql.push_str(" OR ");
        }
        sql.push_str(&format!("fl.path = ?{}", i + 1));
        path_params.push(p.clone());
    }
    sql.push_str(" ORDER BY fl.path, f.line_start");

    let mut stmt = conn.prepare(&sql)?;
    let param_refs: Vec<&dyn rusqlite::types::ToSql> = path_params
        .iter()
        .map(|p| p as &dyn rusqlite::types::ToSql)
        .collect();
    let rows = stmt.query_map(param_refs.as_slice(), |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, i64>(3)?,
            r.get::<_, Option<String>>(4)?,
            r.get::<_, Option<String>>(5)?,
            r.get::<_, Option<String>>(6)?,
        ))
    })?;

    let mut all = Vec::new();
    for row in rows {
        let (name, path, line_start, line_end, args, return_type, body) = row?;
        let priority = ENTRY_PRIORITY
            .iter()
            .position(|p| *p == name.as_str())
            .unwrap_or(usize::MAX);
        all.push((
            priority,
            json!({
                "name": name,
                "path": path,
                "line_start": line_start,
                "line_end": line_end,
                "args": args,
                "return_type": return_type,
            }),
            body,
        ));
    }
    all.sort_by_key(|(prio, _, _)| *prio);

    let mut entry = Vec::new();
    let mut bodies = Vec::new();
    for (i, (_prio, meta, body)) in all.into_iter().enumerate() {
        if entry.len() < PROC_CAP {
            entry.push(meta.clone());
        }
        if deep && bodies.len() < DEEP_BODIES {
            if let Some(b) = body {
                let name = meta["name"].as_str().unwrap_or("").to_string();
                let path = meta["path"].as_str().unwrap_or("").to_string();
                let capped = cap_body(&b);
                bodies.push(json!({
                    "name": name,
                    "path": path,
                    "line_start": meta["line_start"],
                    "line_end": meta["line_end"],
                    "body": capped.text,
                    "body_truncated": capped.truncated,
                }));
            }
        }
        if entry.len() >= PROC_CAP && (!deep || bodies.len() >= DEEP_BODIES) {
            let _ = i;
            break;
        }
    }
    Ok((entry, bodies))
}

struct CappedBody {
    text: String,
    truncated: bool,
}

fn cap_body(body: &str) -> CappedBody {
    let cap = code_index_core::mcp::cap::function_body_cap();
    if cap == 0 || body.chars().count() <= cap {
        return CappedBody {
            text: body.to_string(),
            truncated: false,
        };
    }
    let head: String = body.chars().take(cap / 2).collect();
    let tail: String = body
        .chars()
        .rev()
        .take(cap / 4)
        .collect::<String>()
        .chars()
        .rev()
        .collect();
    CappedBody {
        text: format!(
            "{}\n\n… тело усечено (лимит max_function_body_chars); \
             полное — get_function(name=…) …\n\n{}",
            head, tail
        ),
        truncated: true,
    }
}

fn build_next_hint(
    repo: &str,
    full_name: &str,
    top_form: &str,
    top_proc: &str,
    deep: bool,
) -> String {
    use code_index_core::mcp::cap::next_call_hint;
    let mut parts = Vec::new();
    if !top_proc.is_empty() && !deep {
        parts.push(next_call_hint(
            "get_function",
            &[("repo", json!(repo)), ("function_name", json!(top_proc))],
        ));
        parts.push(format!(
            "или investigate_object(repo='{}', full_name='{}', depth='deep') — до 3 тел точек входа",
            repo, full_name
        ));
    }
    if !top_form.is_empty() {
        parts.push(next_call_hint(
            "get_form_handlers",
            &[
                ("repo", json!(repo)),
                ("owner_full_name", json!(full_name)),
                ("form_name", json!(top_form)),
            ],
        ));
    }
    parts.push(next_call_hint(
        "find_references",
        &[("repo", json!(repo)), ("object", json!(full_name))],
    ));
    parts.push(next_call_hint(
        "get_call_tree",
        &[
            ("repo", json!(repo)),
            ("root", json!(if top_proc.is_empty() { "…" } else { top_proc })),
            ("direction", json!("callees")),
        ],
    ));
    format!(
        "Дальше по необходимости (не вызывайте всё подряд):\n{}",
        parts.join("\n")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summarize_structure_counts_and_samples() {
        let structure = json!({
            "attributes": [
                {"name": "A", "synonym": "А", "type": "String"},
                {"name": "B", "type": "Number"},
            ],
            "tabular_sections": [{"name": "Goods"}],
            "enum_values": []
        });
        let s = summarize_structure(&structure);
        assert_eq!(s["attributes_total"], 2);
        assert_eq!(s["attributes_sample"].as_array().unwrap().len(), 2);
        assert_eq!(s["tabular_sections_total"], 1);
        assert_eq!(s["enum_values_total"], 0);
    }

    #[test]
    fn cap_body_respects_zero_as_disabled() {
        // function_body_cap по умолчанию > 0; при коротком теле truncated=false.
        let c = cap_body("короткое");
        assert!(!c.truncated);
        assert_eq!(c.text, "короткое");
    }
}
