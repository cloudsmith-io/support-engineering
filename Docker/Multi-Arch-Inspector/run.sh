#!/usr/bin/env bash
set -euo pipefail

# Parse args for flags
UNTAGGED=false
UNTAGGED_DELETE=false
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--untagged" ]]; then
    UNTAGGED=true
  elif [[ "$arg" == "--untagged-delete" ]]; then
    UNTAGGED_DELETE=true
  else
    ARGS+=("$arg")
  fi
done
set -- "${ARGS[@]}"

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

# Table Format
TBL_FMT="| %-20s | %-15s | %-30s | %-10s | %-75s |\n"
SEP_LINE="+----------------------+-----------------+--------------------------------+------------+-----------------------------------------------------------------------------+"

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

# authorization header
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
      # echo "No architecture data found."
      # exit 1
      ARCHS=("unknown")
    fi

    local platform="${ARCHS[*]}"

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
      local status_display=""
      for s in "${STATUS[@]}"; do
         case "$s" in
            Completed)     status_display+="${s} ${CHECK} " ;;
            "In Progress") status_display+="${s} ${TIMER} " ;;
            Quarantined)   status_display+="${s} ${VULN} " ;;
            Failed)        status_display+="${s} ${CROSS} " ;;
            *)             status_display+="${s} " ;;
         esac
      done

      local dl=0
      if (( ${#DOWNLOADS[@]} == 3  )); then
        dl=${DOWNLOADS[1]}
      elif (( ${#DOWNLOADS[@]} > 0 )); then
        dl=${DOWNLOADS[0]}
      fi 
      
      totalDownloads=$((totalDownloads+dl))

      printf "$TBL_FMT" "${nTAG}" "${platform}" "${status_display}" "${dl}" "${digest}"
      echo "$SEP_LINE"

    }

    getPackageData "${digest}"

  }


# Get the individual digests for the tag
getDockerDigests () {

  local nTAG="$1"
  local totalDownloads=0
  API_BASE="https://api.cloudsmith.io/v1/packages/${WORKSPACE}/${REPO}/"

  # index_digest="$(curl -fsSL "${AUTH_HEADER[@]}" \
  #   -H "Accept: application/vnd.oci.image.manifest.v1+json" \
  #   -o /dev/null \
  #   -w "%header{Docker-Content-Digest}" \
  #   "${CLOUDSMITH_URL}/v2/${WORKSPACE}/${REPO}/${IMG}/manifests/${nTAG}")"
  
  # echo
  # echo "🐳 ${WORKSPACE}/${REPO}/${IMG}:${nTAG}"
  # echo "   Index Digest: ${index_digest}"
  

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
    # echo "No digests found."
    return
  fi

  for i in "${!DIGESTS[@]}"; do  
    getDigestData "${DIGESTS[i]}"
  done
  # echo "  |___ Total Downloads: ${totalDownloads}"

}

getUntaggedImages() {
  echo "Searching for untagged manifest lists..."
  API_BASE="https://api.cloudsmith.io/v1/packages/${WORKSPACE}/${REPO}/"
  
  # Fetch list
  PACKAGES_JSON="$(curl -sS "${AUTH_HEADER[@]}" \
    -H "Cache-Control: no-cache" \
    --get "${API_BASE}" \
    --data-urlencode "query=name:${IMG}")"

  # Filter for untagged manifest lists
  mapfile -t UNTAGGED_PKGS < <(jq -r '
    .[] 
    | select(.type_display == "manifest/list") 
    | select(.tags.version == null or (.tags.version | length == 0))
    | [ .version, .status_str, (.downloads // 0), .slug ] | @tsv
  ' <<< "${PACKAGES_JSON}")

  if (( ${#UNTAGGED_PKGS[@]} == 0 )); then
    echo "No untagged manifest lists found."
    return
  fi

  echo
  echo "$SEP_LINE"
  printf "$TBL_FMT" "TAG" "PLATFORM" "STATUS" "DOWNLOADS" "DIGEST"
  echo "$SEP_LINE"

  for pkg in "${UNTAGGED_PKGS[@]}"; do
    IFS=$'\t' read -r digest status downloads slug <<< "$pkg"
    
    # Ensure digest has sha256: prefix
    if [[ "$digest" != sha256:* ]]; then
        digest="sha256:${digest}"
    fi

    # Fetch manifest to get platforms
    MANIFEST_JSON="$(curl -L -sS "${AUTH_HEADER[@]}" \
      -H "Accept: application/vnd.oci.image.manifest.v1+json" \
      -H "Cache-Control: no-cache" \
      "${CLOUDSMITH_URL}/v2/${WORKSPACE}/${REPO}/${IMG}/manifests/${digest}")"
      
    mapfile -t ARCHS < <(jq -r '
      if .manifests then
        .manifests[] | ((.platform.os // "linux") + "/" + (.platform.architecture // "unknown"))
      else
        "unknown"
      end
    ' <<< "${MANIFEST_JSON}" | sort -u)
    
    platform="${ARCHS[*]}"
    
    # Format status
    local status_display=""
     case "$status" in
        Completed)     status_display+="${status} ${CHECK} " ;;
        "In Progress") status_display+="${status} ${TIMER} " ;;
        Quarantined)   status_display+="${status} ${VULN} " ;;
        Failed)        status_display+="${status} ${CROSS} " ;;
        *)             status_display+="${status} " ;;
     esac
     
     # Print Parent (Manifest List)
     printf "$TBL_FMT" "(untagged) [List]" "${platform}" "${status_display}" "${downloads}" "${digest}"
     echo "$SEP_LINE"

     # Fetch and Print Children
     mapfile -t DIGESTS < <(jq -r '
        if type=="object" and (.manifests? // empty and (.manifests[].platform.architecture )) then
          .manifests[]?
          | select((.platform.architecture? // "unknown") | ascii_downcase != "unknown")
          | .digest
        else
          .. | objects | .digest? // empty
        end
      ' <<< "${MANIFEST_JSON}" | awk 'NF' | sort -u)

     local nTAG="(untagged)"
     local totalDownloads=0
     for i in "${!DIGESTS[@]}"; do  
        getDigestData "${DIGESTS[i]}"
     done

     if $UNTAGGED_DELETE; then
        echo "   Deleting package: ${slug}..."
        curl -sS -X DELETE "${AUTH_HEADER[@]}" \
          "https://api.cloudsmith.io/v1/packages/${WORKSPACE}/${REPO}/${slug}/"
        echo "   Deleted."
        echo "$SEP_LINE"
     fi
  done
}


# Lookup Docker multi-arch images and output an overview
if $UNTAGGED; then
  getUntaggedImages
else
  getDockerTags
  read -r -a images <<< "$nTAGS"
  echo "Found matching tags: ${#images[@]}"

  for t in "${!images[@]}"; do
    tag=" - ${images[t]}"
    echo "$tag"
  done 

  echo
  echo "$SEP_LINE"
  printf "$TBL_FMT" "TAG" "PLATFORM" "STATUS" "DOWNLOADS" "DIGEST"
  echo "$SEP_LINE"

  for t in "${!images[@]}"; do
    getDockerDigests "${images[t]}"
  done
fi







