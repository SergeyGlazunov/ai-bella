#!/bin/sh
# Content probe for Neo4j metadata graph (R02 / F02).
# Usage: probe_graph.sh present|absent <rel-path> [more paths...]
# Multiple paths share ONE cypher-shell (one JVM) inside the Neo4j container.
# - .bsl  → nodes with source_path
# - .xml  → MetadataObject by full_name_eng derived from KIND_BY_DIR
# Password stays inside container env (not host argv).
# Exit 0 = all checked paths match mode; 1 = confirmed mismatch; 2 = control/uncertain.
set -eu

MODE="${1:?mode present|absent}"
shift
[ "$#" -ge 1 ] || {
  echo "probe_graph: need at least one path" >&2
  exit 2
}
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j}"
NEO4J_USER="${NEO4J_USER:-neo4j}"

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

nested_object_name() {
  rel="$1"
  second=$(printf '%s' "$rel" | awk -F/ 'NF>=2 {print $2}')
  third=$(printf '%s' "$rel" | awk -F/ 'NF>=3 {print $3}')
  [ -n "$second" ] || { printf ''; return; }
  case "$third" in
    Forms|Ext|Templates|Commands|Help|Predefined|Flowcharts|"")
      printf '%s' "$second"
      ;;
    *.xml)
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
}

# Emit one Cypher statement that returns path, count (or empty if unmappable).
cypher_for_path() {
  REL=$(printf '%s' "$1" | tr '\\' '/')
  ESC_PATH=$(printf '%s' "$REL" | sed "s/'/\\\\'/g")
  case "$REL" in
    *.xml)
      FULL=$(xml_to_full_name "$REL")
      if [ -z "$FULL" ]; then
        DIR=$(printf '%s' "$REL" | awk -F/ 'NF>=1 {print $1}')
        OBJ=$(nested_object_name "$REL")
        KIND=$(kind_for_dir "$DIR")
        PREFIX=$(printf '%s' "$REL" | awk -F/ 'NF>=2 {print $1 "/" $2}')
        if [ -z "$OBJ" ] || [ -z "$KIND" ]; then
          return 1
        fi
        ESC_OBJ=$(printf '%s' "$OBJ" | sed "s/'/\\\\'/g")
        ESC_FULL=$(printf '%s.%s' "$KIND" "$OBJ" | sed "s/'/\\\\'/g")
        ESC_PREFIX=$(printf '%s' "$PREFIX" | sed "s/'/\\\\'/g")
        printf "MATCH (n) WHERE (n:MetadataObject AND (n.full_name_eng = '%s' OR n.id = '%s' OR n.name = '%s' OR n.full_name_eng ENDS WITH '.%s')) OR (n.source_path IS NOT NULL AND n.source_path STARTS WITH '%s/') RETURN '%s' AS p, count(n) AS c;\n" \
          "$ESC_FULL" "$ESC_FULL" "$ESC_OBJ" "$ESC_OBJ" "$ESC_PREFIX" "$ESC_PATH"
      else
        ESC_FULL=$(printf '%s' "$FULL" | sed "s/'/\\\\'/g")
        printf "MATCH (n:MetadataObject) WHERE n.full_name_eng = '%s' OR n.id = '%s' RETURN '%s' AS p, count(n) AS c;\n" \
          "$ESC_FULL" "$ESC_FULL" "$ESC_PATH"
      fi
      ;;
    *)
      ESC=$(printf '%s' "$REL" | sed "s/'/\\\\'/g")
      printf "MATCH (n) WHERE n.source_path IS NOT NULL AND (n.source_path ENDS WITH '%s' OR n.source_path = '%s') RETURN '%s' AS p, count(n) AS c;\n" \
        "$ESC" "$ESC" "$ESC_PATH"
      ;;
  esac
  return 0
}

QUERIES=""
CHECKED=0
SKIPPED=0
for REL in "$@"; do
  if q=$(cypher_for_path "$REL"); then
    QUERIES="${QUERIES}${q}"
    CHECKED=$((CHECKED + 1))
  else
    echo "probe_graph: skip unmappable path: $REL" >&2
    SKIPPED=$((SKIPPED + 1))
  fi
done

if [ "$CHECKED" -eq 0 ]; then
  echo "probe_graph: no mappable paths (skipped=$SKIPPED)" >&2
  exit 2
fi

set +e
raw="$(printf '%s' "$QUERIES" | docker exec -i "$NEO4J_CONTAINER" sh -c \
  "PASS=\"\${HC_NEO4J_PASSWORD:-\${NEO4J_PASSWORD:-}}\"; \
   if [ -z \"\$PASS\" ]; then echo 'probe_graph: no password in container env' >&2; exit 2; fi; \
   cypher-shell -u \"$NEO4J_USER\" -p \"\$PASS\" --format plain" 2>&1)"
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
  echo "probe_graph: control failure rc=$rc" >&2
  echo "$raw" >&2
  exit 2
fi

# plain rows look like: p, c  /  path, N  (header may repeat per statement)
fail=0
while IFS= read -r line; do
  case "$line" in
    p,*|\"p\"*) continue ;;
  esac
  path=$(printf '%s' "$line" | awk -F',' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); gsub(/^"|"$/, "", $1); print $1}')
  count=$(printf '%s' "$line" | awk -F',' '{gsub(/[^0-9]/, "", $2); print $2+0}')
  [ -n "$path" ] || continue
  case "$MODE" in
    present)
      if [ "$count" -le 0 ]; then
        echo "probe_graph: expected present: $path (count=$count)" >&2
        fail=1
      fi
      ;;
    absent)
      if [ "$count" -gt 0 ]; then
        echo "probe_graph: expected absent: $path (count=$count)" >&2
        fail=1
      fi
      ;;
    *)
      echo "probe_graph: bad mode $MODE" >&2
      exit 2
      ;;
  esac
done <<EOF
$raw
EOF

if [ "$fail" -ne 0 ]; then
  echo "$raw" >&2
  exit 1
fi
exit 0
