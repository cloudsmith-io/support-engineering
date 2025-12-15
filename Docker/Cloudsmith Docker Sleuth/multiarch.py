#!/usr/bin/env python3

import sys
import os
import json
import argparse
import urllib.request
import urllib.error
from urllib.parse import urlencode
import concurrent.futures
import time

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
                            time.sleep(wait + 0.5)

                if method == 'DELETE':
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
                time.sleep(wait_time + 0.5)
                continue
            elif e.code == 404:
                return None
            else:
                return None
        except Exception as e:
            return None
    
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

# --- Core Logic ---

def get_digest_data(workspace, repo, img, digest, ntag_display, platform="unknown"):
    """Fetches data for a specific digest (child image) and returns row data."""
    
    # 1. Fetch Manifest to get Architecture (Only if unknown)
    if platform == "unknown":
        manifest_url = f"{CLOUDSMITH_URL}/v2/{workspace}/{repo}/{img}/manifests/{digest}"
        manifest_json = make_request(manifest_url, {"Accept": "application/vnd.oci.image.manifest.v1+json", "Cache-Control": "no-cache"})
        
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
    
    status_display = ""
    dl = 0
    
    if pkg_details:
        statuses = set(find_key_recursive(pkg_details, 'status_str'))
        status_parts = [format_status(s) for s in statuses]
        status_display = " ".join(status_parts)

        downloads = find_key_recursive(pkg_details, 'downloads')
        if len(downloads) >= 2:
             dl = downloads[0]
        elif len(downloads) > 0:
            dl = downloads[0]

    # Return tuple of (Row Columns List, Download Count)
    row_data = [
        f"  └─ {ntag_display}", 
        "image", 
        platform, 
        status_display, 
        str(dl), 
        f"[dim]{digest}[/dim]"
    ]
    return row_data, dl

def fetch_tag_data(workspace, repo, img, ntag, detailed=False):
    """Fetches the manifest list for a tag and returns rows for the table."""
    
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
    children_rows = []
    total_downloads = 0
    
    for child in children:
        row, dl = get_digest_data(workspace, repo, img, child['digest'], ntag, platform=child['platform'])
        children_rows.append(row)
        total_downloads += dl

    # Fetch parent package info
    api_url = f"https://api.cloudsmith.io/v1/packages/{workspace}/{repo}/?query=version:{ntag}"
    pkg_details = make_request(api_url, {"Cache-Control": "no-cache"})
    
    parent_status = "Unknown"
    index_digest = ""
    
    if pkg_details and len(pkg_details) > 0:
        parent_status = pkg_details[0].get('status_str', 'Unknown')
        ver = pkg_details[0].get('version', '')
        if ver and not ver.startswith('sha256:'):
            index_digest = f"sha256:{ver}"
        else:
            index_digest = ver

    status_display = format_status(parent_status)

    rows = []
    # Parent Row
    rows.append([
        f"[bold cyan]{ntag}[/bold cyan]",
        "[magenta]manifest/list[/magenta]",
        "multi",
        status_display,
        f"[green]{total_downloads}[/green]",
        f"[dim]{index_digest}[/dim]"
    ])

    # Children Rows
    if detailed:
        rows.extend(children_rows)
        rows.append("SECTION")

    return rows

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

    status_display = format_status(status)

    rows = []
    rows.append([
        "(untagged)", 
        "manifest/list", 
        platform_str, 
        status_display, 
        f"[green]{downloads}[/green]", 
        digest
    ])

    if detailed:
        for child in child_digests:
            row, _ = get_digest_data(workspace, repo, img, child['digest'], "(untagged)", platform=child['platform'])
            rows.append(row)
        rows.append("SECTION")
        
    return rows, slug

def get_untagged_images(workspace, repo, img, delete=False, detailed=False):
    # console.print("[bold]Searching for untagged manifest lists...[/bold]") # Removed print
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
        # console.print("[yellow]No untagged manifest lists found.[/yellow]") # Removed print
        return None

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
    if delete and packages_to_delete:
        batch_size = 10
        def delete_pkg_task(slug):
            del_url = f"https://api.cloudsmith.io/v1/packages/{workspace}/{repo}/{slug}/"
            return slug, make_request(del_url, method='DELETE')

        for i in range(0, len(packages_to_delete), batch_size):
            batch = packages_to_delete[i:i + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = [executor.submit(delete_pkg_task, slug) for slug in batch]
                for future in concurrent.futures.as_completed(futures):
                    slug, success = future.result()
                    if success:
                        deleted_slugs.add(slug)
            
            if i + batch_size < len(packages_to_delete):
                time.sleep(1.1)

    # Build Table
    table = Table(title=f"Untagged Manifest Lists: {img}", box=box.ROUNDED)
    table.add_column("Tag", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("Downloads", justify="right")
    table.add_column("Digest", style="dim")
    if delete:
        table.add_column("Action", style="bold red")

    for i in range(len(untagged_pkgs)):
        if i in results_map:
            rows, slug = results_map[i]
            
            action_str = ""
            if delete:
                if slug in deleted_slugs:
                    action_str = "Deleted"
                else:
                    action_str = "Failed"

            for row in rows:
                if row == "SECTION":
                    table.add_section()
                else:
                    if delete:
                        table.add_row(*row, action_str)
                    else:
                        table.add_row(*row)
    
    return table

def get_image_analysis(workspace, repo, img_name, detailed=False):
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
        return None

    table = Table(title=f"Image Analysis: {img_name}", box=box.ROUNDED)
    table.add_column("Tag", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("Downloads", justify="right")
    table.add_column("Digest", style="dim")

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
                rows = results[t]
                for row in rows:
                    if row == "SECTION":
                        table.add_section()
                    else:
                        table.add_row(*row)
    return table

def process_image(org, repo, img_name, args):
    if args.untagged or args.untagged_delete:
        return get_untagged_images(org, repo, img_name, delete=args.untagged_delete, detailed=args.detailed)
    else:
        return get_image_analysis(org, repo, img_name, detailed=args.detailed)

def main():
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
    parser.add_argument("--detailed", action="store_true", help="Show detailed breakdown of digests")

    args = parser.parse_args()

    images_to_scan = []

    if args.img:
        images_to_scan.append(args.img)
    else:
        console.print(f"[bold]Fetching catalog for {args.org}/{args.repo}...[/bold]")
        catalog_url = f"{CLOUDSMITH_URL}/v2/{args.org}/{args.repo}/_catalog"
        catalog_json = make_request(catalog_url, {"Accept": "application/json", "Cache-Control": "no-cache"})
        
        if catalog_json and 'repositories' in catalog_json:
            images_to_scan = catalog_json['repositories']
        else:
            console.print("[red]Failed to fetch catalog or no images found.[/red]")
            sys.exit(1)

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
                    table = future.result()
                    if table:
                        collected_results.append((img_name, table))
                    else:
                        # Optional: log empty/no tags
                        pass
                except Exception as e:
                    progress.console.print(f"[red]Error processing {img_name}: {e}[/red]")
                
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
        console.print("[yellow]No matching images or tags found.[/yellow]")

    for _, table in collected_results:
        console.print(table)
        console.print("")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold red]Operation cancelled by user.[/bold red]")
        # Use os._exit to avoid