#!/bin/sh
# Content probe for Neo4j metadata graph (R02 / F02).
# Usage: probe_graph.sh present|absent <rel-path>
# - .bsl  → nodes with source_path
# - .xml  → MetadataObject by full_name_eng derived from KIND_BY_DIR
# Password stays inside container env (not host argv).
# Exit 2 = control failure (never treat as confirmed absent).
set -eu

MODE="${1:?mode present|absent}"
REL="${2:?relative path}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"

REL=$(printf '%s' "$REL" | tr '\\' '/')
ESC=$(printf '%s' "$REL" | sed "s/'/\\\\'/g")

# Map top-level dump dirs → MetadataObject kind (english), aligned with indexer KINDS.
xml_to_full_name() {
  # stdin: relative xml path → stdout: full_name_eng or empty
  rel="$1"
  case "$rel" in
    Catalogs/*.xml)      base=${rel#Catalogs/}; base=${base%.xml}; printf 'Catalog.%s' "$base" ;;
    Documents/*.xml)     base=${rel#Documents/}; base=${base%.xml}; printf 'Document.%s' "$base" ;;
    CommonModules/*.xml) base=${rel#CommonModules/}; base=${base%.xml}; printf 'CommonModule.%s' "$base" ;;
    InformationRegisters/*.xml) base=${rel#InformationRegisters/}; base=${base%.xml}; printf 'InformationRegister.%s' "$base" ;;
    AccumulationRegisters/*.xml) base=${rel#AccumulationRegisters/}; base=${base%.xml}; printf 'AccumulationRegister.%s' "$base" ;;
    AccountingRegisters/*.xml) base=${rel#AccountingRegisters/}; base=${base%.xml}; printf 'AccountingRegister.%s' "$base" ;;
    CalculationRegisters/*.xml) base=${rel#CalculationRegisters/}; base=${base%.xml}; printf 'CalculationRegister.%s' "$base" ;;
    ChartsOfAccounts/*.xml) base=${rel#ChartsOfAccounts/}; base=${base%.xml}; printf 'ChartOfAccounts.%s' "$base" ;;
    ChartsOfCharacteristicTypes/*.xml) base=${rel#ChartsOfCharacteristicTypes/}; base=${base%.xml}; printf 'ChartOfCharacteristicTypes.%s' "$base" ;;
    ChartsOfCalculationTypes/*.xml) base=${rel#ChartsOfCalculationTypes/}; base=${base%.xml}; printf 'ChartOfCalculationTypes.%s' "$base" ;;
    BusinessProcesses/*.xml) base=${rel#BusinessProcesses/}; base=${base%.xml}; printf 'BusinessProcess.%s' "$base" ;;
    Tasks/*.xml) base=${rel#Tasks/}; base=${base%.xml}; printf 'Task.%s' "$base" ;;
    Enums/*.xml) base=${rel#Enums/}; base=${base%.xml}; printf 'Enum.%s' "$base" ;;
    Reports/*.xml) base=${rel#Reports/}; base=${base%.xml}; printf 'Report.%s' "$base" ;;
    DataProcessors/*.xml) base=${rel#DataProcessors/}; base=${base%.xml}; printf 'DataProcessor.%s' "$base" ;;
    ExchangePlans/*.xml) base=${rel#ExchangePlans/}; base=${base%.xml}; printf 'ExchangePlan.%s' "$base" ;;
    FilterCriteria/*.xml) base=${rel#FilterCriteria/}; base=${base%.xml}; printf 'FilterCriterion.%s' "$base" ;;
    SettingsStorages/*.xml) base=${rel#SettingsStorages/}; base=${base%.xml}; printf 'SettingsStorage.%s' "$base" ;;
    Subsystems/*.xml) base=${rel#Subsystems/}; base=${base%.xml}; printf 'Subsystem.%s' "$base" ;;
    Roles/*.xml) base=${rel#Roles/}; base=${base%.xml}; printf 'Role.%s' "$base" ;;
    CommonForms/*.xml) base=${rel#CommonForms/}; base=${base%.xml}; printf 'CommonForm.%s' "$base" ;;
    CommonTemplates/*.xml) base=${rel#CommonTemplates/}; base=${base%.xml}; printf 'CommonTemplate.%s' "$base" ;;
    CommonPictures/*.xml) base=${rel#CommonPictures/}; base=${base%.xml}; printf 'CommonPicture.%s' "$base" ;;
    Constants/*.xml) base=${rel#Constants/}; base=${base%.xml}; printf 'Constant.%s' "$base" ;;
    DefinedTypes/*.xml) base=${rel#DefinedTypes/}; base=${base%.xml}; printf 'DefinedType.%s' "$base" ;;
    SessionParameters/*.xml) base=${rel#SessionParameters/}; base=${base%.xml}; printf 'SessionParameter.%s' "$base" ;;
    FunctionalOptions/*.xml) base=${rel#FunctionalOptions/}; base=${base%.xml}; printf 'FunctionalOption.%s' "$base" ;;
    *) printf '' ;;
  esac
}

case "$REL" in
  *.xml)
    FULL=$(xml_to_full_name "$REL")
    if [ -z "$FULL" ]; then
      # Nested XML (Forms/…): match parent object via path prefix of source_path modules,
      # or MetadataObject name = second path segment.
      PARENT=$(printf '%s' "$REL" | awk -F/ 'NF>=2 {print $2}')
      ESC_FULL=$(printf '%s' "$PARENT" | sed "s/'/\\\\'/g")
      CYPHER="MATCH (n:MetadataObject) WHERE n.name = '${ESC_FULL}' OR n.full_name_eng ENDS WITH '.${ESC_FULL}' RETURN count(n) AS c;"
    else
      ESC_FULL=$(printf '%s' "$FULL" | sed "s/'/\\\\'/g")
      CYPHER="MATCH (n:MetadataObject) WHERE n.full_name_eng = '${ESC_FULL}' OR n.id = '${ESC_FULL}' RETURN count(n) AS c;"
    fi
    ;;
  *)
    # Prefer exact ENDS WITH; avoid ultra-broad CONTAINS alone for short names.
    CYPHER="MATCH (n) WHERE n.source_path IS NOT NULL AND (n.source_path ENDS WITH '${ESC}' OR n.source_path = '${ESC}') RETURN count(n) AS c;"
    ;;
esac

set +e
raw="$(docker exec "$NEO4J_CONTAINER" sh -c \
  "PASS=\"\${HC_NEO4J_PASSWORD:-\${NEO4J_PASSWORD:-}}\"; \
   if [ -z \"\$PASS\" ]; then echo 'probe_graph: no password in container env' >&2; exit 2; fi; \
   cypher-shell -u \"$NEO4J_USER\" -p \"\$PASS\" --format plain \"$CYPHER\"" 2>&1)"
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
  echo "probe_graph: control failure rc=$rc" >&2
  echo "$raw" >&2
  exit 2
fi

count="$(printf '%s\n' "$raw" | tr -cd '0-9\n' | awk 'NF{v=$0} END{print v+0}')"

case "$MODE" in
  present)
    if [ "$count" -gt 0 ]; then
      exit 0
    fi
    echo "probe_graph: expected present: $REL (count=$count)" >&2
    echo "$raw" >&2
    exit 1
    ;;
  absent)
    if [ "$count" -eq 0 ]; then
      exit 0
    fi
    echo "probe_graph: expected absent: $REL (count=$count)" >&2
    echo "$raw" >&2
    exit 1
    ;;
  *)
    echo "probe_graph: bad mode $MODE" >&2
    exit 2
    ;;
esac
