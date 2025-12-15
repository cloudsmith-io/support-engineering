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
    """Fetches data for a specific digest (child image) and returns data dict."""
    
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
    
    if pkg_details and len(pkg_details) > 0:
        parent_status = pkg_details[0].get('status_str', 'Unknown')
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
        "is_child": False
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
            data = get_digest_data(workspace, repo, img, child['digest'], "(untagged)", platform=child['platform'])
            results.append(data)
        
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
                    else:
                        failed_slugs.add(slug)
            
            if i + batch_size < len(packages_to_delete):
                time.sleep(1.1)

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
                row['action'] = action_str
                # Remove internal slug
                if 'slug' in row: del row['slug']
            
            groups.append(rows)
    
    return groups

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
    return groups

def process_image(org, repo, img_name, args):
    if args.untagged or args.untagged_delete:
        return get_untagged_images(org, repo, img_name, delete=args.untagged_delete, detailed=args.detailed)
    else:
        return get_image_analysis(org, repo, img_name, detailed=args.detailed)

def render_table(image_name, groups, is_untagged=False, has_action=False):
    title = f"Untagged Manifest Lists: {image_name}" if is_untagged else f"Image Analysis: {image_name}"
    table = Table(title=title, box=box.ROUNDED)
    table.add_column("Tag", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Platform")
    table.add_column("Status")
    table.add_column("Downloads", justify="right")
    table.add_column("Digest", style="dim")
    if has_action:
        table.add_column("Action", style="bold red")

    for i, group in enumerate(groups):
        if i > 0:
            table.add_section()
        
        for row in group:
            # Format for Table
            tag_display = row['tag']
            if row['is_child']:
                tag_display = f"  └─ {row['tag']}"
            else:
                tag_display = f"[bold cyan]{row['tag']}[/bold cyan]"
            
            type_display = row['type']
            if type_display == 'manifest/list':
                type_display = "[magenta]manifest/list[/magenta]"
            
            status_display = format_status(row['status'])
            
            dl_display = str(row['downloads'])
            if row['type'] == 'manifest/list':
                dl_display = f"[green]{dl_display}[/green]"
            
            digest_display = f"[dim]{row['digest']}[/dim]"
            
            row_data = [tag_display, type_display, row['platform'], status_display, dl_display, digest_display]
            
            if has_action:
                row_data.append(row.get('action', ''))
            
            table.add_row(*row_data)
            
    return table

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
    parser.add_argument("--output", choices=['table', 'json', 'csv'], default='table', help="Output format (default: table)")

    args = parser.parse_args()

    images_to_scan = []

    if args.img:
        images_to_scan.append(args.img)
    else:
        if args.output == 'table':
            console.print(f"[bold]Fetching catalog for {args.org}/{args.repo}...[/bold]")
        catalog_url = f"{CLOUDSMITH_URL}/v2/{args.org}/{args.repo}/_catalog"
        catalog_json = make_request(catalog_url, {"Accept": "application/json", "Cache-Control": "no-cache"})
        
        if catalog_json and 'repositories' in catalog_json:
            images_to_scan = catalog_json['repositories']
        else:
            if args.output == 'table':
                console.print("[red]Failed to fetch catalog or no images found.[/red]")
            sys.exit(1)

    # Only show progress bar for table output
    if args.output == 'table':
        progress_ctx = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        )
    else:
        # Dummy context manager for non-table output
        class DummyProgress:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def add_task(self, *args, **kwargs): return None
            def advance(self, *args, **kwargs): pass
            @property
            def console(self): return console # fallback
        progress_ctx = DummyProgress()

    collected_results = []

    with progress_ctx as progress:
        if args.output == 'table':
            task = progress.add_task(f"Processing {len(images_to_scan)} images...", total=len(images_to_scan))
        
        # Use a reasonable number of workers for images (e.g., 5)
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
                    if args.output == 'table':
                        progress.console.print(f"[red]Error processing {img_name}: {e}[/red]")
                
                if args.output == 'table':
                    progress.advance(task)
            
            executor.shutdown(wait=True)
            
        except KeyboardInterrupt:
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    # Sort results by image name
    collected_results.sort(key=lambda x: x[0])
    
    if not collected_results:
        if args.output == 'table':
            console.print("[yellow]No matching images or tags found.[/yellow]")
        elif args.output == 'json':
            print("[]")
        return

    # --- Output Handling ---

    if args.output == 'table':
        for img_name, groups in collected_results:
            is_untagged = args.untagged or args.untagged_delete
            has_action = args.untagged_delete
            table = render_table(img_name, groups, is_untagged, has_action)
            console.print(table)
            console.print("")

    elif args.output == 'json':
        # Flatten structure for JSON: List of objects, each with 'image' field
        json_output = []
        for img_name, groups in collected_results:
            for group in groups:
                for row in group:
                    row_copy = row.copy()
                    row_copy['image'] = img_name
                    json_output.append(row_copy)
        print(json.dumps(json_output, indent=2))

    elif args.output == 'csv':
        # Flatten structure for CSV
        csv_rows = []
        fieldnames = ['image', 'tag', 'type', 'platform', 'status', 'downloads', 'digest', 'is_child', 'action']
        
        for img_name, groups in collected_results:
            for group in groups:
                for row in group:
                    row_copy = row.copy()
                    row_copy['image'] = img_name
                    # Ensure all fields exist
                    for f in fieldnames:
                        if f not in row_copy:
                            row_copy[f] = ''
                    csv_rows.append(row_copy)
        
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if 'console' in globals():
            console.print("\n[bold red]Operation cancelled by user.[/bold red]")
        else:
            print("\nOperation cancelled by user.")
        sys.exit(1)