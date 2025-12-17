#!/usr/bin/env python3

import sys
import os
import json
import csv
import argparse
import urllib.request
import urllib.error
from urllib.parse import urlencode
import concurrent.futures
import time
import logging
from datetime import datetime

# Try to import rich
try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
except ImportError:
    print("Error: This script requires the 'rich' library.")
    print("Please install it using: pip install rich")
    sys.exit(1)

# --- Configuration & Constants ---

console = Console()

# API Config
CLOUDSMITH_URL = os.environ.get("CLOUDSMITH_URL", "https://docker.cloudsmith.io")
API_KEY = os.environ.get("CLOUDSMITH_API_KEY")
AUTH_HEADER = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

# --- Logging Setup ---
def setup_logging(debug_mode=False):
    log_filename = "multiarch_inspector.log"
    level = logging.DEBUG if debug_mode else logging.INFO
    
    # Reset handlers to avoid duplicate logs if called multiple times
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            root.removeHandler(handler)
            
    logging.basicConfig(
        filename=log_filename,
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return logging.getLogger()

# Initialize with default INFO level, will be re-initialized in main
logger = setup_logging()

# --- Helper Functions ---

def make_request(url, headers=None, method='GET', data=None):
    """Performs an HTTP request and returns parsed JSON. Handles rate limiting."""
    if headers is None:
        headers = {}
    
    final_headers = {**AUTH_HEADER, **headers}
    
    req = urllib.request.Request(url, headers=final_headers, method=method)
    if data:
        req.data = data.encode('utf-8')

    max_retries = 5
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req) as response:
                # Proactive Rate Limit Handling via Headers
                # https://docs.cloudsmith.com/api/rate-limits#monitoring-your-usage
                remaining = response.headers.get('X-RateLimit-Remaining')
                if remaining is not None and int(remaining) < 3:
                    reset = response.headers.get('X-RateLimit-Reset')
                    if reset:
                        wait = float(reset) - time.time()
                        if wait > 0 and wait < 30: # Only sleep if wait is reasonable
                            logger.warning(f"Rate limit approaching. Sleeping for {wait:.2f}s")
                            time.sleep(wait + 0.5)

                if method == 'DELETE':
                    logger.info(f"DELETE Success: {url}")
                    return True
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited - wait and retry
                retry_after = e.headers.get('Retry-After')
                if retry_after:
                    wait_time = float(retry_after)
                else:
                    # Fallback to X-RateLimit-Reset
                    reset = e.headers.get('X-RateLimit-Reset')
                    if reset:
                        wait_time = float(reset) - time.time()
                    else:
                        wait_time = (2 ** attempt)
                
                if wait_time < 0: wait_time = 1
                logger.warning(f"Rate Limited (429). Retrying in {wait_time:.2f}s. URL: {url}")
                time.sleep(wait_time + 0.5)
                continue
            elif e.code == 404:
                logger.debug(f"404 Not Found: {url}")
                return None
            else:
                logger.error(f"HTTP Error {e.code}: {url}")
                return None
        except Exception as e:
            logger.error(f"Request Error: {e} - URL: {url}")
            return None
    
    logger.error(f"Max retries exceeded for: {url}")
    return None

def find_key_recursive(obj, key):
    """Recursively searches for a key in a dictionary/list and returns a list of values."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                results.append(v)
            elif isinstance(v, (dict, list)):
                results.extend(find_key_recursive(v, key))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_key_recursive(item, key))
    return results

def format_status(status_str):
    """Returns a rich-formatted status string."""
    if status_str == "Completed": return f"[green]{status_str}[/green] ✅"
    if status_str == "In Progress": return f"[yellow]{status_str}[/yellow] ⏳"
    if status_str == "Quarantined": return f"[red]{status_str}[/red] ☠️"
    if status_str == "Failed": return f"[bold red]{status_str}[/bold red] ❌"
    return status_str

def batch_delete_packages(workspace, repo, slugs):
    """Deletes a list of package slugs in batches to respect rate limits."""
    deleted = set()
    failed = set()
    if not slugs:
        return deleted, failed
        
    logger.info(f"Starting batch deletion for {len(slugs)} packages.")
    batch_size = 10
    def delete_pkg_task(slug):
        del_url = f"https://api.cloudsmith.io/v1/packages/{workspace}/{repo}/{slug}/"
        return slug, make_request(del_url, method='DELETE')

    for i in range(0, len(slugs), batch_size):
        batch = slugs[i:i + batch_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = [executor.submit(delete_pkg_task, slug) for slug in batch]
            for future in concurrent.futures.as_completed(futures):
                slug, success = future.result()
                if success:
                    deleted.add(slug)
                    logger.info(f"Deleted package slug: {slug}")
                else:
                    failed.add(slug)
                    logger.error(f"Failed to delete package slug: {slug}")
        
        if i + batch_size < len(slugs):
            time.sleep(1.1)
            
    return deleted, failed

# --- Core Logic ---

def get_digest_data(workspace, repo, img, digest, ntag_display, platform="unknown"):
    """Fetches data for a specific digest (child image) and returns data dict."""
    
    # 1. Fetch Manifest to get Architecture (Only if unknown)
    if platform == "unknown":
        manifest_url = f"{CLOUDSMITH_URL}/v2/{workspace}/{repo}/{img}/manifests/{digest}"
        manifest_json = make_request(manifest_url, {"Accept": "application/vnd.oci.image.manifest.v2+json", "Cache-Control": "no-cache"})
        
        if manifest_json:
            if 'manifests' in manifest_json:
                found = False
                for m in manifest_json['manifests']:
                    if m.get('digest') == digest:
                        p = m.get('platform', {})
                        platform = f"{p.get('os', '')}/{p.get('architecture', '')}"
                        found = True
                        break
                if not found:
                    archs = find_key_recursive(manifest_json, 'architecture')
                    if archs: platform = archs[0]
            else:
                archs = find_key_recursive(manifest_json, 'architecture')
                if archs: platform = archs[0]

    # 2. Get Package Data from API
    version = digest.replace("sha256:", "")
    api_url = f"https://api.cloudsmith.io/v1/packages/{workspace}/{repo}/?query=version:{version}"
    pkg_details = make_request(api_url, {"Cache-Control": "no-cache"})
    
    status_raw = "Unknown"
    dl = 0
    
    if pkg_details:
        statuses = set(find_key_recursive(pkg_details, 'status_str'))
        if statuses:
            status_raw = " ".join(sorted(list(statuses)))

        downloads = find_key_recursive(pkg_details, 'downloads')
        if len(downloads) >= 2:
             dl = downloads[0]
        elif len(downloads) > 0:
            dl = downloads[0]

    return {
        "tag": ntag_display,
        "type": "image",
        "platform": platform,
        "status": status_raw,
        "downloads": dl,
        "digest": digest,
        "is_child": True
    }

def fetch_tag_data(workspace, repo, img, ntag, detailed=False):
    """Fetches the manifest list for a tag and returns a list of data dicts."""
    
    manifest_url = f"{CLOUDSMITH_URL}/v2/{workspace}/{repo}/{img}/manifests/{ntag}"
    manifest_json = make_request(manifest_url, {"Accept": "application/vnd.oci.image.manifest.v1+json", "Cache-Control": "no-cache"})
    
    if not manifest_json:
        return []

    # Parse out digests and platforms
    children = []
    if 'manifests' in manifest_json:
        for m in manifest_json['manifests']:
            d = m.get('digest')
            p = m.get('platform', {})
            os_name = p.get('os', 'linux')
            arch = p.get('architecture', 'unknown')
            plat = f"{os_name}/{arch}"
            
            if d and arch.lower() != 'unknown':
                children.append({'digest': d, 'platform': plat})
    else:
        # Fallback
        digests = list(set(find_key_recursive(manifest_json, 'digest')))
        for d in digests:
             children.append({'digest': d, 'platform': 'unknown'})

    if not children:
        return []

    # Process children
    children_data = []
    total_downloads = 0
    
    for child in children:
        data = get_digest_data(workspace, repo, img, child['digest'], ntag, platform=child['platform'])
        children_data.append(data)
        total_downloads += data['downloads']

    # Fetch parent package info
    api_url = f"https://api.cloudsmith.io/v1/packages/{workspace}/{repo}/?query=version:{ntag}"
    pkg_details = make_request(api_url, {"Cache-Control": "no-cache"})
    
    parent_status = "Unknown"
    index_digest = ""
    slug = ""
    
    if pkg_details and len(pkg_details) > 0:
        parent_status = pkg_details[0].get('status_str', 'Unknown')
        slug = pkg_details[0].get('slug', '')
        ver = pkg_details[0].get('version', '')
        if ver and not ver.startswith('sha256:'):
            index_digest = f"sha256:{ver}"
        else:
            index_digest = ver

    results = []
    # Parent Data
    results.append({
        "tag": ntag,
        "type": "manifest/list",
        "platform": "multi",
        "status": parent_status,
        "downloads": total_downloads,
        "digest": index_digest,
        "is_child": False,
        "slug": slug
    })

    # Children Data
    if detailed:
        results.extend(children_data)

    return results

def fetch_untagged_data(pkg, workspace, repo, img, detailed=False):
    digest = pkg.get('version')
    if digest and not digest.startswith('sha256:'):
        digest = f"sha256:{digest}"
    
    status = pkg.get('status_str')
    downloads = pkg.get('downloads', 0)
    slug = pkg.get('slug')
    
    # Fetch manifest to get platforms
    manifest_url = f"{CLOUDSMITH_URL}/v2/{workspace}/{repo}/{img}/manifests/{digest}"
    manifest_json = make_request(manifest_url, {"Accept": "application/vnd.oci.image.manifest.v1+json", "Cache-Control": "no-cache"})
    
    child_digests = []
    platform_str = "unknown"

    if manifest_json:
        archs = set()
        if 'manifests' in manifest_json:
            for m in manifest_json['manifests']:
                p = m.get('platform', {})
                os_name = p.get('os', 'linux')
                arch = p.get('architecture', 'unknown')
                plat = f"{os_name}/{arch}"
                archs.add(plat)
                
                if arch.lower() != 'unknown':
                    child_digests.append({'digest': m['digest'], 'platform': plat})
        else:
            archs.add("unknown")
        
        platform_str = " ".join(sorted(list(archs)))

    results = []
    results.append({
        "tag": "(untagged)",
        "type": "manifest/list",
        "platform": platform_str,
        "status": status,
        "downloads": downloads,
        "digest": digest,
        "is_child": False,
        "slug": slug # Internal use
    })

    if detailed:
        for child in child_digests:
            # FIX: get_digest_data returns a dict, not a tuple
            row = get_digest_data(workspace, repo, img, child['digest'], "(untagged)", platform=child['platform'])
            results.append(row)
        results.append("SECTION")
        
    return results, slug

def get_untagged_images(workspace, repo, img, delete=False, detailed=False):
    api_url = f"https://api.cloudsmith.io/v1/packages/{workspace}/{repo}/"
    query = urlencode({'query': f"name:{img}"})
    full_url = f"{api_url}?{query}"
    
    packages = make_request(full_url, {"Cache-Control": "no-cache"})
    
    untagged_pkgs = []
    if packages:
        for p in packages:
            if p.get('type_display') == 'manifest/list':
                tags = p.get('tags', {})
                if not tags.get('version'):
                    untagged_pkgs.append(p)

    if not untagged_pkgs:
        return None
    
    logger.info(f"Found {len(untagged_pkgs)} untagged manifest lists for image: {img}")

    # Fetch data first
    results_map = {}
    packages_to_delete = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_untagged_data, pkg, workspace, repo, img, detailed): i for i, pkg in enumerate(untagged_pkgs)}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                rows, slug = future.result()
                results_map[index] = (rows, slug)
                packages_to_delete.append(slug)
            except Exception:
                pass

    # Perform Deletion if requested
    deleted_slugs = set()
    failed_slugs = set()
    if delete and packages_to_delete:
        deleted_slugs, failed_slugs = batch_delete_packages(workspace, repo, packages_to_delete)

    # Build Result Groups
    groups = []
    for i in range(len(untagged_pkgs)):
        if i in results_map:
            rows, slug = results_map[i]
            
            # Update action status
            action_str = ""
            if delete:
                if slug in deleted_slugs:
                    action_str = "Deleted"
                elif slug in failed_slugs:
                    action_str = "Failed"
            
            for row in rows:
                if isinstance(row, dict):
                    row['action'] = action_str
                    # Remove internal slug
                    if 'slug' in row: del row['slug']
            
            groups.append(rows)
    
    return groups

def get_image_analysis(workspace, repo, img_name, delete_all=False, delete_tag=None, detailed=False):
    tags_url = f"{CLOUDSMITH_URL}/v2/{workspace}/{repo}/{img_name}/tags/list"
    tags_json = make_request(tags_url, {"Accept": "application/vnd.oci.image.manifest.v1+json", "Cache-Control": "no-cache"})
    
    tags = []
    if tags_json:
        raw_tags = find_key_recursive(tags_json, 'tags')
        flat_tags = []
        for item in raw_tags:
            if isinstance(item, list):
                flat_tags.extend(item)
            else:
                flat_tags.append(item)
        
        tags = sorted(list(set(flat_tags)))

    if not tags:
        logger.info(f"No tags found for image: {img_name}")
        return None

    groups = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_tag = {executor.submit(fetch_tag_data, workspace, repo, img_name, t, detailed): t for t in tags}
        
        results = {}
        for future in concurrent.futures.as_completed(future_to_tag):
            tag = future_to_tag[future]
            try:
                results[tag] = future.result()
            except Exception:
                pass
        
        for t in tags:
            if t in results:
                groups.append(results[t])

    # Deletion Logic for Tagged Images
    packages_to_delete = []
    for group in groups:
        if not group: continue
        parent = group[0]
        # Only delete manifest lists
        if parent.get('type') == 'manifest/list':
            should_delete = False
            if delete_all:
                should_delete = True
            elif delete_tag and parent.get('tag') == delete_tag:
                should_delete = True
            
            if should_delete and parent.get('slug'):
                packages_to_delete.append(parent['slug'])

    if packages_to_delete:
        logger.info(f"Identified {len(packages_to_delete)} tagged packages to delete for image: {img_name}")

    deleted_slugs = set()
    failed_slugs = set()
    if packages_to_delete:
        deleted_slugs, failed_slugs = batch_delete_packages(workspace, repo, packages_to_delete)

    # Update Action Status in Groups
    for group in groups:
        if not group: continue
        parent = group[0]
        slug = parent.get('slug')
        
        action_str = ""
        if slug in deleted_slugs:
            action_str = "Deleted"
        elif slug in failed_slugs:
            action_str = "Failed"
        
        if action_str:
            parent['action'] = action_str
            # Optionally propagate to children if needed, but usually just parent row
            for row in group:
                if isinstance(row, dict):
                    row['action'] = action_str

    return groups

def process_image(org, repo, img_name, args):
    if args.untagged or args.untagged_delete:
        return get_untagged_images(org, repo, img_name, delete=args.untagged_delete, detailed=args.detailed)
    else:
        return get_image_analysis(org, repo, img_name, delete_all=args.delete_all, delete_tag=args.delete_tag, detailed=args.detailed)

def render_table(image_name, groups, is_untagged=False, has_action=False):
    # --- Table Setup ---
    table = Table(title=f"{'Untagged' if is_untagged else 'Tagged'} Image Analysis: {image_name}", box=box.ROUNDED)
    table.add_column("Tag", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("Downloads", justify="right")
    table.add_column("Digest", style="dim")
    if has_action:
        table.add_column("Action", style="bold red")

    # --- Row Rendering ---
    for group in groups:
        if not group: continue
        parent = group[0]
        
        # Action string for delete status
        action_str = parent.get('action', "")
        
        # Parent Row
        if is_untagged:
            table.add_row(
                parent.get("tag", ""),
                parent.get("type", ""),
                parent.get("platform", ""),
                format_status(parent.get("status", "")),
                f"[green]{parent.get('downloads', 0)}[/green]",
                f"[dim]{parent.get('digest', '')}[/dim]",
                action_str if has_action else None
            )
        else:
            row_data = [
                f"[bold cyan]{parent.get('tag', '')}[/bold cyan]",
                "[magenta]manifest/list[/magenta]",
                "multi",
                format_status(parent.get("status", "")),
                f"[green]{parent.get('downloads', 0)}[/green]",
                f"[dim]{parent.get('digest', '')}[/dim]"
            ]
            if has_action:
                row_data.append(action_str)
            
            table.add_row(*row_data)

        # Child Rows
        if len(group) > 1:
            for row in group[1:]:
                if row == "SECTION":
                    table.add_section()
                else:
                    row_data = [
                        f"  └─ {row.get('tag', '')}",
                        row.get("type", ""),
                        row.get("platform", ""),
                        format_status(row.get("status", "")),
                        f"[green]{row.get('downloads', 0)}[/green]",
                        f"[dim]{row.get('digest', '')}[/dim]"
                    ]
                    if has_action:
                        row_data.append(row.get('action', ''))
                    
                    table.add_row(*row_data)
    
    return table

def main():
    # Parse args first to configure logging
    parser = argparse.ArgumentParser(description="Docker Multi-Arch Inspector")
    parser.add_argument("org", help="Cloudsmith Organization/User")
    parser.add_argument("repo", help="Cloudsmith Repository")
    parser.add_argument("img", nargs="?", help="Image Name (Optional - if omitted, scans all images)")
    parser.add_argument("--untagged", action="store_true", help="Find untagged manifest lists")
    parser.add_argument("--untagged-delete", action="store_true", help="Delete untagged manifest lists")
    parser.add_argument("--delete-all", action="store_true", help="Delete ALL detected manifest lists")
    parser.add_argument("--delete-tag", help="Delete manifest lists matching this specific tag")
    parser.add_argument("--detailed", action="store_true", help="Show detailed breakdown of digests")
    parser.add_argument("--output", choices=['table', 'json', 'csv'], default='table', help="Output format (default: table)")
    parser.add_argument("--debug-log", action="store_true", help="Enable debug logging to file")

    args = parser.parse_args()

    # Re-configure logging based on args
    global logger
    logger = setup_logging(args.debug_log)

    logger.info("--- Script Started ---")
    console.print(r"""[bold cyan]
██████╗██╗      ██████╗ ██╗   ██╗██████╗ ███████╗███╗   ███╗██╗████████╗██╗  ██╗
██╔════╝██║     ██╔═══██╗██║   ██║██╔══██╗██╔════╝████╗ ████║██║╚══██╔══╝██║  ██║
██║     ██║     ██║   ██║██║   ██║██║  ██║███████╗██╔████╔██║██║   ██║   ███████║
██║     ██║     ██║   ██║██║   ██║██║  ██║╚════██║██║╚██╔╝██║██║   ██║   ██╔══██║
╚██████╗███████╗╚██████╔╝╚██████╔╝██████╔╝███████║██║ ╚═╝ ██║██║   ██║   ██║  ██║
 ╚═════╝╚══════╝ ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝   ╚═╝   ╚═╝  ╚═╝

██████╗  ██████╗  ██████╗██╗  ██╗███████╗██████╗     ███████╗██╗     ███████╗██╗   ██╗████████╗██╗  ██╗
██╔══██╗██╔═══██╗██╔════╝██║ ██╔╝██╔════╝██╔══██╗    ██╔════╝██║     ██╔════╝██║   ██║╚══██╔══╝██║  ██║
██║  ██║██║   ██║██║     █████╔╝ █████╗  ██████╔╝    ███████╗██║     █████╗  ██║   ██║   ██║   ███████║
██║  ██║██║   ██║██║     ██╔═██╗ ██╔══╝  ██╔══██╗    ╚════██║██║     ██╔══╝  ██║   ██║   ██║   ██╔══██║
██████╔╝╚██████╔╝╚██████╗██║  ██╗███████╗██║  ██║    ███████║███████╗███████╗╚██████╔╝   ██║   ██║  ██║
╚═════╝  ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝    ╚══════╝╚══════╝╚══════╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝
[/bold cyan]""")

    parser = argparse.ArgumentParser(description="Docker Multi-Arch Inspector")
    parser.add_argument("org", help="Cloudsmith Organization/User")
    parser.add_argument("repo", help="Cloudsmith Repository")
    parser.add_argument("img", nargs="?", help="Image Name (Optional - if omitted, scans all images)")
    parser.add_argument("--untagged", action="store_true", help="Find untagged manifest lists")
    parser.add_argument("--untagged-delete", action="store_true", help="Delete untagged manifest lists")
    parser.add_argument("--delete-all", action="store_true", help="Delete ALL detected manifest lists")
    parser.add_argument("--delete-tag", help="Delete manifest lists matching this specific tag")
    parser.add_argument("--detailed", action="store_true", help="Show detailed breakdown of digests")
    parser.add_argument("--output", choices=['table', 'json', 'csv'], default='table', help="Output format (default: table)")
    parser.add_argument("--debug-log", action="store_true", help="Enable debug logging to file")

    args = parser.parse_args()
    logger.info(f"Arguments: {args}")

    images_to_scan = []

    if args.img:
        images_to_scan.append(args.img)
    else:
        if args.output == 'table':
            console.print(f"[bold]Fetching catalog for {args.org}/{args.repo}...[/bold]")
        
        logger.info(f"Fetching catalog for {args.org}/{args.repo}")
        catalog_url = f"{CLOUDSMITH_URL}/v2/{args.org}/{args.repo}/_catalog"
        catalog_json = make_request(catalog_url, {"Accept": "application/json", "Cache-Control": "no-cache"})
        
        if catalog_json and 'repositories' in catalog_json:
            images_to_scan = catalog_json['repositories']
            logger.info(f"Found {len(images_to_scan)} images in catalog.")
        else:
            msg = "Failed to fetch catalog or no images found."
            if args.output == 'table':
                console.print(f"[red]{msg}[/red]")
            logger.error(msg)
            sys.exit(1)

    # Only show progress bar for table output
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task = progress.add_task(f"Processing {len(images_to_scan)} images...", total=len(images_to_scan))
        
        collected_results = []

        # Use a reasonable number of workers for images (e.g., 5)
        # Each image might spawn its own threads for tags/digests
        # Manually manage executor to handle KeyboardInterrupt gracefully
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
        try:
            future_to_img = {
                executor.submit(process_image, args.org, args.repo, img, args): img 
                for img in images_to_scan
            }
            
            for future in concurrent.futures.as_completed(future_to_img):
                img_name = future_to_img[future]
                try:
                    groups = future.result()
                    if groups:
                        collected_results.append((img_name, groups))
                except Exception as e:
                    logger.error(f"Error processing {img_name}: {e}")
                    if args.output == 'table':
                        progress.console.print(f"[red]Error processing {img_name}: {e}[/red]")
                
                if args.output == 'table':
                    progress.advance(task)
            
            # Normal shutdown
            executor.shutdown(wait=True)
            
        except KeyboardInterrupt:
            # Force shutdown without waiting
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    # Sort results by image name and print
    collected_results.sort(key=lambda x: x[0])
    
    if not collected_results:
        if args.output == 'table':
            console.print("[yellow]No matching images or tags found.[/yellow]")
        elif args.output == 'json':
            print("[]")
        logger.info("No matching images or tags found.")
        return

    # --- Output Handling ---

    if args.output == 'table':
        for img_name, groups in collected_results:
            is_untagged = args.untagged or args.untagged_delete
            has_action = args.untagged_delete or args.delete_all or (args.delete_tag is not None)
            table = render_table(image_name=img_name, groups=groups, is_untagged=is_untagged, has_action=has_action)
            console.print(table)
            console.print("")
    elif args.output == 'json':
        # JSON Output for all images
        all_results = {}
        for img_name, groups in collected_results:
            all_results[img_name] = groups
        
        json_output = json.dumps(all_results, indent=2)
        console.print(json_output)
    elif args.output == 'csv':
        # CSV Output (simple flat structure)
        csv_lines = []
        csv_lines.append(["Image", "Tag", "Type", "Platform", "Status", "Downloads", "Digest", "Action"]) # Header
        
        for img_name, groups in collected_results:
            for group in groups:
                if group == "SECTION":
                    continue
                # Flat CSV row
                csv_lines.append([
                    img_name,
                    group.get("tag", ""),
                    group.get("type", ""),
                    group.get("platform", ""),
                    group.get("status", ""),
                    str(group.get("downloads", 0)),
                    group.get("digest", ""),
                    group.get("action", "")
                ])
        
        # Print CSV
        for line in csv_lines:
            console.print(",".join(f'"{str(item)}"' for item in line))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold red]Operation cancelled by user.[/bold red]")
        # Use os._exit to avoid hanging on shutdown
        os._exit(0)