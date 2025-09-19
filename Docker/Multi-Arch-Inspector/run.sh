#!/usr/bin/env bash
set -euo pipefail

# Usage: ./run.sh <org> <repo> <img>
# Requires: curl, jq
# Auth: export CLOUDSMITH_API_KEY=<your_token> 

# Color setup (auto-disable if not a TTY or tput missing)
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  GREEN="$(tput setaf 2)"; RED="$(tput setaf 1)"; RESET="$(tput sgr0)"
else
  GREEN=""; RED=""; RESET=""
fi

# Icons (fallback to ASCII if not on UTF-8 locale)
CHECK='✅'; CROSS='❌'; TIMER='⏳'; VULN='☠️'
case ${LC_ALL:-${LC_CTYPE:-$LANG}} in *UTF-8*|*utf8*) : ;; *) CHECK='OK'; CROSS='X' ;; esac

completed() { printf '%s%s%s %s\n' "$GREEN" "$CHECK" "$RESET" "$*"; }
progress()  { printf '%s%s%s %s\n' "$YELLOW" "$TIMER" "$RESET" "$*"; }
quarantined() { printf '%s%s%s %s\n' "$ORANGE" "$VULN" "$RESET" "$*"; }
fail() { printf '%s%s%s %s\n' "$RED"   "$CROSS" "$RESET" "$*"; }

CLOUDSMITH_URL="${1:-}"
WORKSPACE="${2:-}"
REPO="${3:-}"
IMG="${4:-}"

if [[ -z "${CLOUDSMITH_URL}" ]]; then
  CLOUDSMITH_URL="https://docker.cloudsmith.io"
fi

# uthorization header
AUTH_HEADER=()
if [[ -n "${CLOUDSMITH_API_KEY:-}" ]]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${CLOUDSMITH_API_KEY}")
fi

echo
echo "Docker Image: ${WORKSPACE}/${REPO}/${IMG}"


# 1) Get all associated tags from the repo for the image
getDockerTags () {

  # 1) Get all applicable tags for the image
  echo
    TAGS_JSON="$(curl -L -sS "${AUTH_HEADER[@]}" \
    -H "Accept: application/vnd.oci.image.manifest.v1+json" \
    -H "Cache-Control: no-cache" \
    "${CLOUDSMITH_URL}/v2/${WORKSPACE}/${REPO}/${IMG}/tags/list")"

  mapfile -t TAGS < <(jq -r '
    if type=="object" then
      .tags[]
    else
      .. | objects | .tags? // empty
    end
  ' <<< "${TAGS_JSON}" | awk 'NF' | sort -u)

  if (( ${#TAGS[@]} == 0 )); then
    echo "No tags found for the image."
    exit 1
  fi

  nTAGS="${TAGS[@]}"

}

getDigestData () {

    local digest="$1"
    mapfile -t ARCHS < <(jq -r --arg d "${digest}" '
      if type=="object" and (.manifests? // empty) then
        .manifests[]?
        | select(.digest == $d )
        | ((.platform.os // "") + "/" + (.platform.architecture // ""))
      else
        .. | objects | .architecture? // empty
      end
    ' <<< "${MANIFEST_JSON}" | awk 'NF' | sort -u)

    if (( ${#ARCHS[@]} == 0 )); then
      echo "No architecture data found."
      exit 1
    fi

    # Get the package data from Cloudsmith API packages list endpoint
    getPackageData () {

      #echo "Fetching data for the images."
      local digest="$1"
      local version="${digest#*:}" # Strip sha256: from string

      # Get package data using the query string "version:<digest>"
      PKG_DETAILS="$(curl -sS "${AUTH_HEADER[@]}" \
        -H "Cache-Control: no-cache" \
        --get "${API_BASE}?query=version:${version}")"

      mapfile -t STATUS < <(jq -r '
        .. | objects | .status_str
        ' <<< "${PKG_DETAILS}" | awk 'NF' | sort -u)

      mapfile -t DOWNLOADS < <(jq -r '
        .. | objects | .downloads
        ' <<< "${PKG_DETAILS}" | awk 'NF' | sort -u)

      
      # handle the different status's
      case "${STATUS[0]}" in
        Completed)
          echo "          |____ Status: ${STATUS[0]} ${CHECK}" 
          ;;

        "In Progress")
          echo "          |____ Status: ${STATUS[0]} ${TIMER}" 
          ;;

        Quarantined)
          echo "          |____ Status: ${STATUS[1]} ${VULN}" 
          ;;

        Failed)
          echo "          |____ Status: ${STATUS[0]} ${FAIL}" 
          ;;
        
      esac

      case "${STATUS[1]}" in
        Completed)
          echo "          |____ Status: ${STATUS[1]} ${CHECK}" 
          ;;

        "In Progress")
          echo "          |____ Status: ${STATUS[1]} ${TIMER}" 
          ;;

        Quarantined)
          echo "          |____ Status: ${STATUS[1]} ${VULN}" 
          ;;

        Failed)
          echo "          |____ Status: ${STATUS[1]} ${FAIL}" 
          ;;
        
      esac

      if (( ${#DOWNLOADS[@]} == 3  )); then
        echo "          |____ Downloads: ${DOWNLOADS[1]}"
        count=${DOWNLOADS[1]}
        totalDownloads=$((totalDownloads+count))
      else 
        echo "          |____ Downloads: ${DOWNLOADS[0]}"
      fi 

    }

    echo "        - ${digest}"
    echo "        - Platform: ${ARCHS}"
    getPackageData "${digest}"

  }


# Get the individual digests for the tag
getDockerDigests () {

  local nTAG="$1"
  local totalDownloads=0
  API_BASE="https://api.cloudsmith.io/v1/packages/${WORKSPACE}/${REPO}/"

  index_digest="$(curl -fsSL "${AUTH_HEADER[@]}" \
    -H "Accept: application/vnd.oci.image.manifest.v1+json" \
    -o /dev/null \
    -w "%header{Docker-Content-Digest}" \
    "${CLOUDSMITH_URL}/v2/${WORKSPACE}/${REPO}/${IMG}/manifests/${nTAG}")"
  
  echo
  echo "🐳 ${WORKSPACE}/${REPO}/${IMG}:${nTAG}"
  echo "   Index Digest: ${index_digest}"
  

  MANIFEST_JSON="$(curl -L -sS "${AUTH_HEADER[@]}" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json" \
  -H "Cache-Control: no-cache" \
  "${CLOUDSMITH_URL}/v2/${WORKSPACE}/${REPO}/${IMG}/manifests/${nTAG}")"


  # Parse out digest(s) and architectures
  #    - Prefer `.manifests[].digest` (typical manifest list)
  #    - Fallback to any `.digest` fields if needed, then unique
  mapfile -t DIGESTS < <(jq -r '
    if type=="object" and (.manifests? // empty and (.manifests[].platform.architecture )) then
      .manifests[]?
      | select((.platform.architecture? // "unknown") | ascii_downcase != "unknown")
      | .digest
    else
      .. | objects | .digest? // empty
    end
  ' <<< "${MANIFEST_JSON}" | awk 'NF' | sort -u)

  if (( ${#DIGESTS[@]} == 0 )); then
    echo "No digests found."
    exit 1
  fi

  for i in "${!DIGESTS[@]}"; do  
    echo
    getDigestData "${DIGESTS[i]}"
    echo
  done
  echo "  |___ Total Downloads: ${totalDownloads}"

}


# Lookup Docker multi-arch images and output an overview
getDockerTags
read -r -a images <<< "$nTAGS"
echo "Found matching tags:"
echo
for t in "${!images[@]}"; do
  tag=" - ${images[t]}"
  echo "$tag"
done 

echo
for t in "${!images[@]}"; do
  getDockerDigests "${images[t]}"
done 







