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
# Shell `case` globs match `/`, so Catalogs/X/Forms/.../Form.xml would otherwise
# become Catalog.X/Forms/.../Form — reject any base that still contains `/`.
kind_for_dir() {
  case "$1" in
    Catalogs) printf 'Catalog' ;;
    Documents) printf 'Document' ;;
    CommonModules) printf 'CommonModule' ;;
    InformationRegisters) printf 'InformationRegister' ;;
    AccumulationRegisters) printf 'AccumulationRegister' ;;
    AccountingRegisters) printf 'AccountingRegister' ;;
    CalculationRegisters) printf 'CalculationRegister' ;;
    ChartsOfAccounts) printf 'ChartOfAccounts' ;;
    ChartsOfCharacteristicTypes) printf 'ChartOfCharacteristicTypes' ;;
    ChartsOfCalculationTypes) printf 'ChartOfCalculationTypes' ;;
    BusinessProcesses) printf 'BusinessProcess' ;;
    Tasks) printf 'Task' ;;
    Enums) printf 'Enum' ;;
    Reports) printf 'Report' ;;
    DataProcessors) printf 'DataProcessor' ;;
    ExchangePlans) printf 'ExchangePlan' ;;
    FilterCriteria) printf 'FilterCriterion' ;;
    SettingsStorages) printf 'SettingsStorage' ;;
    Subsystems) printf 'Subsystem' ;;
    Roles) printf 'Role' ;;
    CommonForms) printf 'CommonForm' ;;
    CommonTemplates) printf 'CommonTemplate' ;;
    CommonPictures) printf 'CommonPicture' ;;
    Constants) printf 'Constant' ;;
    DefinedTypes) printf 'DefinedType' ;;
    SessionParameters) printf 'SessionParameter' ;;
    FunctionalOptions) printf 'FunctionalOption' ;;
    *) printf '' ;;
  esac
}

xml_to_full_name() {
  # arg: relative xml path → stdout: full_name_eng or empty
  rel="$1"
  kind=""
  base=""
  case "$rel" in
    Catalogs/*.xml)      kind=Catalog; base=${rel#Catalogs/} ;;
    Documents/*.xml)     kind=Document; base=${rel#Documents/} ;;
    CommonModules/*.xml) kind=CommonModule; base=${rel#CommonModules/} ;;
    InformationRegisters/*.xml) kind=InformationRegister; base=${rel#InformationRegisters/} ;;
    AccumulationRegisters/*.xml) kind=AccumulationRegister; base=${rel#AccumulationRegisters/} ;;
    AccountingRegisters/*.xml) kind=AccountingRegister; base=${rel#AccountingRegisters/} ;;
    CalculationRegisters/*.xml) kind=CalculationRegister; base=${rel#CalculationRegisters/} ;;
    ChartsOfAccounts/*.xml) kind=ChartOfAccounts; base=${rel#ChartsOfAccounts/} ;;
    ChartsOfCharacteristicTypes/*.xml) kind=ChartOfCharacteristicTypes; base=${rel#ChartsOfCharacteristicTypes/} ;;
    ChartsOfCalculationTypes/*.xml) kind=ChartOfCalculationTypes; base=${rel#ChartsOfCalculationTypes/} ;;
    BusinessProcesses/*.xml) kind=BusinessProcess; base=${rel#BusinessProcesses/} ;;
    Tasks/*.xml) kind=Task; base=${rel#Tasks/} ;;
    Enums/*.xml) kind=Enum; base=${rel#Enums/} ;;
    Reports/*.xml) kind=Report; base=${rel#Reports/} ;;
    DataProcessors/*.xml) kind=DataProcessor; base=${rel#DataProcessors/} ;;
    ExchangePlans/*.xml) kind=ExchangePlan; base=${rel#ExchangePlans/} ;;
    FilterCriteria/*.xml) kind=FilterCriterion; base=${rel#FilterCriteria/} ;;
    SettingsStorages/*.xml) kind=SettingsStorage; base=${rel#SettingsStorages/} ;;
    Subsystems/*.xml) kind=Subsystem; base=${rel#Subsystems/} ;;
    Roles/*.xml) kind=Role; base=${rel#Roles/} ;;
    CommonForms/*.xml) kind=CommonForm; base=${rel#CommonForms/} ;;
    CommonTemplates/*.xml) kind=CommonTemplate; base=${rel#CommonTemplates/} ;;
    CommonPictures/*.xml) kind=CommonPicture; base=${rel#CommonPictures/} ;;
    Constants/*.xml) kind=Constant; base=${rel#Constants/} ;;
    DefinedTypes/*.xml) kind=DefinedType; base=${rel#DefinedTypes/} ;;
    SessionParameters/*.xml) kind=SessionParameter; base=${rel#SessionParameters/} ;;
    FunctionalOptions/*.xml) kind=FunctionalOption; base=${rel#FunctionalOptions/} ;;
    *) printf ''; return ;;
  esac
  base=${base%.xml}
  case "$base" in
    ""|*/*) printf '' ;;
    *) printf '%s.%s' "$kind" "$base" ;;
  esac
}

# Nested XML under KindDir/ObjectName/(Forms|Ext|…)/… → parent object name.
# Nested subsystem Subsystems/Parent/Child.xml → Child (leaf stem), not Parent.
nested_object_name() {
  rel="$1"
  dir=$(printf '%s' "$rel" | awk -F/ 'NF>=1 {print $1}')
  second=$(printf '%s' "$rel" | awk -F/ 'NF>=2 {print $2}')
  third=$(printf '%s' "$rel" | awk -F/ 'NF>=3 {print $3}')
  [ -n "$second" ] || { printf ''; return; }
  case "$third" in
    Forms|Ext|Templates|Commands|Help|Predefined|Flowcharts|"")
      printf '%s' "$second"
      ;;
    *.xml)
      # Subsystems/A/B.xml (and similar depth-3 object xml)
      stem=${third%.xml}
      case "$stem" in
        ""|*/*) printf '%s' "$second" ;;
        *) printf '%s' "$stem" ;;
      esac
      ;;
    *)
      printf '%s' "$second"
      ;;
  esac
  # silence unused when awk empty
  : "$dir"
}

case "$REL" in
  *.xml)
    FULL=$(xml_to_full_name "$REL")
    if [ -z "$FULL" ]; then
      DIR=$(printf '%s' "$REL" | awk -F/ 'NF>=1 {print $1}')
      OBJ=$(nested_object_name "$REL")
      KIND=$(kind_for_dir "$DIR")
      PREFIX=$(printf '%s' "$REL" | awk -F/ 'NF>=2 {print $1 "/" $2}')
      if [ -z "$OBJ" ] || [ -z "$KIND" ]; then
        echo "probe_graph: nested xml not mappable: $REL" >&2
        exit 2
      fi
      ESC_OBJ=$(printf '%s' "$OBJ" | sed "s/'/\\\\'/g")
      ESC_FULL=$(printf '%s.%s' "$KIND" "$OBJ" | sed "s/'/\\\\'/g")
      ESC_PREFIX=$(printf '%s' "$PREFIX" | sed "s/'/\\\\'/g")
      # Parent/leaf MetadataObject, or any module under the object folder.
      CYPHER="MATCH (n) WHERE (n:MetadataObject AND (n.full_name_eng = '${ESC_FULL}' OR n.id = '${ESC_FULL}' OR n.name = '${ESC_OBJ}' OR n.full_name_eng ENDS WITH '.${ESC_OBJ}')) OR (n.source_path IS NOT NULL AND n.source_path STARTS WITH '${ESC_PREFIX}/') RETURN count(n) AS c;"
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
