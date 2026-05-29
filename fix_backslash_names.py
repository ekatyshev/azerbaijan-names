#!/usr/bin/env python3
"""
fix_backslash_names.py

Processes OSM objects whose `name` tag contains a backslash:
  - Strips everything from the first backslash onward (keeps left part).
  - Produces an OSM changeset file ready for upload.
  - Produces a CSV with one row per changed object.

Usage:
    python fix_backslash_names.py \
        --input  objects_with_backslash.osm \
        --places all_places.osm \
        --out-osm  changeset.osm \
        --out-csv  report.csv

All four arguments are required. The two input files are never modified.
"""

import argparse
import csv
import math
import re
import sys
from collections import Counter
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres (accuracy ~0.3 %)."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def split_on_first_backslash(name: str):
    """
    Split *name* on the first backslash.

    Rule: one or more consecutive backslashes count as a single separator.
    Returns (left, right) — both stripped — or (name, None) if no backslash.

    Examples
    --------
    'Foo Bar \\ Baku'   -> ('Foo Bar', 'Baku')
    'Foo \\\\ Baku'     -> ('Foo', 'Baku')   (double backslash = one separator)
    'No slash here'     -> ('No slash here', None)
    """
    # Match first run of one-or-more backslashes
    m = re.search(r' \\+ ', name)
    if m is None:
        return name.strip(), None
    left = name[:m.start()].strip()
    right = name[m.end():].strip()
    return left, right if right else None


def osm_object_url(obj_type: str, obj_id: str) -> str:
    return f"https://www.openstreetmap.org/{obj_type}/{obj_id}"


def get_center(elem) -> tuple[float, float] | tuple[None, None]:
    """Return (lat, lon) for node/way/relation, or (None, None) if not available."""
    tag = elem.tag
    if tag == 'node':
        lat = elem.get('lat')
        lon = elem.get('lon')
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    # Ways and relations from a plain .osm export rarely carry centre coordinates;
    # if a <center> child is present (Overpass style) use it.
    center = elem.find('center')
    if center is not None:
        lat = center.get('lat')
        lon = center.get('lon')
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    return None, None


# ---------------------------------------------------------------------------
# Load settlements
# ---------------------------------------------------------------------------

def load_places(path: str) -> list[dict]:
    """
    Parse the places OSM file and return a list of dicts:
        {id, type, name, lat, lon}
    Only objects with a `name` tag and known coordinates are kept.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    places = []
    for elem in root:
        if elem.tag not in ('node', 'way', 'relation'):
            continue
        name = None
        for tag in elem.findall('tag'):
            if tag.get('k') == 'name':
                name = tag.get('v', '').strip()
                break
        if not name:
            continue
        lat, lon = get_center(elem)
        if lat is None:
            continue
        places.append({
            'id':   elem.get('id'),
            'type': elem.tag,
            'name': name,
            'lat':  lat,
            'lon':  lon,
        })
    return places


def find_best_place(second_part: str, places: list[dict]) -> dict | None:
    """
    Return the place whose name exactly matches *second_part* (case-sensitive,
    full-string). If multiple match, the first in file order is returned.
    Returns None when there is no match.
    """
    if not second_part:
        return None
    for p in places:
        if p['name'] == second_part:
            return p
    return None


# ---------------------------------------------------------------------------
# Build changeset XML
# ---------------------------------------------------------------------------

_ATTR_ORDER = [
    'id', 'action', 'timestamp', 'uid', 'user',
    'visible', 'version', 'changeset', 'lat', 'lon',
]


def build_element(orig_elem, new_name: str, second_part: str) -> ET.Element:
    """
    Create a new XML element in JOSM upload format from *orig_elem*,
    replacing the `name` tag value with *new_name*.
    """
    obj_type = orig_elem.tag

    # Collect original attributes
    attrs = dict(orig_elem.attrib)

    # Build ordered attribute dict for the output element
    out_attrs: dict[str, str] = {}

    # id always first
    out_attrs['id'] = attrs.get('id', '')
    out_attrs['action'] = 'modify'

    # Carry over metadata attributes in a stable order
    for key in ('timestamp', 'uid', 'user'):
        if key in attrs:
            out_attrs[key] = attrs[key]

    out_attrs['visible'] = 'true'

    for key in ('version', 'changeset'):
        if key in attrs:
            out_attrs[key] = attrs[key]

    # Geometry (nodes only; ways/relations keep their children)
    if obj_type == 'node':
        for key in ('lat', 'lon'):
            if key in attrs:
                out_attrs[key] = attrs[key]

    new_elem = ET.Element(obj_type, out_attrs)
    new_elem.tail = '\n  '

    # Copy children: <tag> elements (replacing name value), <nd>, <member>, …
    for child in orig_elem:
        if child.tag == 'tag' and child.get('k') == 'name':
            t = ET.SubElement(new_elem, 'tag', {'k': 'name', 'v': new_name})
            original_name = child.get('v')
        elif child.tag == 'center':
            # skip Overpass-only helper element
            continue
        else:
            t = ET.SubElement(new_elem, child.tag, dict(child.attrib))
        t.tail = '\n    '

    # Add a new tag with the original name
    original_name_tag = ET.SubElement(new_elem, 'tag', {'k': 'note:original_name', 'v': original_name})
    original_name_tag.tail = '\n    '

    # Add a new tag with the name in the second part (for easier review)
    second_part_tag = ET.SubElement(new_elem, 'tag', {'k': 'note:second_part', 'v': second_part})
    second_part_tag.tail = '\n    '

    # Trim trailing whitespace on last child
    children = list(new_elem)
    if children:
        children[-1].tail = '\n  '

    return new_elem


def write_changeset(elements: list[ET.Element], out_path: str) -> None:
    root = ET.Element('osm', {'version': '0.6', 'generator': 'fix_backslash_names.py'})
    root.text = '\n  '
    root.tail = '\n'
    for elem in elements:
        root.append(elem)

    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write("<?xml version='1.0' encoding='UTF-8'?>\n")
        # Write element by element to use single-quoted attributes (JOSM style)
        _write_osm_single_quotes(root, f)


def _write_osm_single_quotes(root: ET.Element, f) -> None:
    """Write OSM XML with single-quoted attribute values (JOSM convention)."""

    def _attrs(elem: ET.Element) -> str:
        parts = []
        for k, v in elem.attrib.items():
            escaped = v.replace("'", "&apos;").replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            # avoid double-escaping
            escaped = v.replace('&', '&amp;').replace("'", "&apos;").replace('<', '&lt;').replace('>', '&gt;')
            parts.append(f"{k}='{escaped}'")
        return (' ' + ' '.join(parts)) if parts else ''

    def _write(elem: ET.Element, indent: int) -> None:
        pad = '  ' * indent
        children = list(elem)
        if not children:
            f.write(f"{pad}<{elem.tag}{_attrs(elem)} />\n")
        else:
            f.write(f"{pad}<{elem.tag}{_attrs(elem)}>\n")
            for child in children:
                _write(child, indent + 1)
            f.write(f"{pad}</{elem.tag}>\n")

    _write(root, 0)


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process(input_path: str, places_path: str, out_osm: str, out_csv: str) -> None:
    # --- Parse input file ---
    tree = ET.parse(input_path)
    root = tree.getroot()

    # --- Load places ---
    print(f"Loading places from '{places_path}' …", file=sys.stderr)
    places = load_places(places_path)
    print(f"  {len(places)} places with coordinates loaded.", file=sys.stderr)

    second_part_counter: Counter = Counter()
    changeset_elements: list[ET.Element] = []
    csv_rows: list[dict] = []

    obj_types = ('node', 'way', 'relation')

    for elem in root:
        if elem.tag not in obj_types:
            continue

        # Find `name` tag
        name_tag = None
        for tag in elem.findall('tag'):
            if tag.get('k') == 'name':
                name_tag = tag
                break
        if name_tag is None:
            continue

        original_name = name_tag.get('v', '')
        left, right = split_on_first_backslash(original_name)

        if right is None:
            # No backslash — skip (shouldn't happen given the input query,
            # but guard defensively)
            continue

        second_part_counter[right] += 1

        obj_id   = elem.get('id')
        obj_type = elem.tag
        lat, lon = get_center(elem)

        # Build changeset element
        new_elem = build_element(elem, left, right)
        changeset_elements.append(new_elem)

        # Place matching
        matched_place = find_best_place(right, places)
        place_url  = osm_object_url(matched_place['type'], matched_place['id']) if matched_place else ''
        distance_km = ''
        if matched_place and lat is not None:
            distance_km = round(haversine_km(lat, lon, matched_place['lat'], matched_place['lon']), 2)

        csv_rows.append({
            'object_url':    osm_object_url(obj_type, obj_id),
            'lat':           lat if lat is not None else '',
            'lon':           lon if lon is not None else '',
            'original_name': original_name,
            'new_name':      left,
            'second_part':   right,
            'number_of_occurrences': second_part_counter[right],
            'place_url':     place_url,
            'distance_km':   distance_km,
        })

    # --- Update occurrence counts in CSV rows ---
    for row in csv_rows:
        row['number_of_occurrences'] = second_part_counter[row['second_part']]

    # --- Write OSM changeset ---
    write_changeset(changeset_elements, out_osm)
    print(f"Changeset written to '{out_osm}' ({len(changeset_elements)} objects).", file=sys.stderr)

    # --- Write CSV ---
    fieldnames = [
        'object_url', 'lat', 'lon',
        'original_name', 'new_name', 'second_part', 'number_of_occurrences',
        'place_url', 'distance_km',
    ]
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"CSV written to '{out_csv}' ({len(csv_rows)} rows).", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='Fix OSM name tags containing backslashes and produce a changeset.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('--input',   required=True, metavar='FILE',
                   help='OSM file with objects whose name tags contain backslashes')
    p.add_argument('--places',  required=True, metavar='FILE',
                   help='OSM file with settlement/place objects')
    p.add_argument('--out-osm', required=True, metavar='FILE',
                   help='Output OSM changeset file path')
    p.add_argument('--out-csv', required=True, metavar='FILE',
                   help='Output CSV report file path')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()
    process(
        input_path  = args.input,
        places_path = args.places,
        out_osm     = args.out_osm,
        out_csv     = args.out_csv,
    )
