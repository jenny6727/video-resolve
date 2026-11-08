#!/usr/bin/env python3
"""Generate the iOS Shortcut file (.shortcut) for John's video downloader.

The Shortcut flow:
  1. Get Clipboard (the copied video link)
  2. POST {base}/resolve?key={api_key}  JSON {"url": "<clipboard>"}
  3. Get dictionary value "url" (direct media url)
  4. Get Contents of that URL (the video file)
  5. Save to Photo Album
  6. Show notification

Usage:
  python3 build_shortcut.py --base-url https://xxx.hf.space --api-key KEY --out 下载视频.shortcut

Note: the .shortcut format is hand-built from the Shortcuts plist format.
It is validated here only for structure (plist parses, actions present).
First import on a real iPhone is the true test; if Shortcuts reports an
error on import, fix from that message.
"""
import argparse
import plistlib
import uuid


def new_uuid():
    return str(uuid.uuid4()).upper()


def text_token(string, attachments):
    """A WF text-token string in the modern (iOS 13+) form:
    {"Value": {"string": ..., "attachmentsByRange": {"{start, length}":
      {"OutputName": ..., "OutputUUID": ..., "Type": "ActionOutput"}}},
     "WFSerializationType": "WFTextTokenString"}.
    A literal has an empty attachmentsByRange; a variable token has
    string "\ufffc" (U+FFFC) with one attachment covering it.
    attachments: list of (start_index, length, variable_uuid, output_name).
    NOTE: the older {"Type": "Variable", "Variable": {...}} wrapper renders
    in the editor but evaluates to an empty string at runtime, which is
    what made Shortcuts report "未指定URL"."""
    ranges = {}
    for start, length, var_uuid, output_name in attachments:
        ranges["{%d, %d}" % (start, length)] = {
            "OutputName": output_name,
            "OutputUUID": var_uuid,
            "Type": "ActionOutput",
        }
    return {
        "Value": {"string": string, "attachmentsByRange": ranges},
        "WFSerializationType": "WFTextTokenString",
    }


def variable_value(var_uuid, output_name):
    """A text field holding only a variable: same modern token form."""
    return text_token("\ufffc", [(0, 1, var_uuid, output_name)])


def dict_field_value(pairs):
    items = []
    for k, v in pairs:
        items.append({
            "WFItemType": 0,
            "WFKey": text_token(k, []),
            "WFValue": v if isinstance(v, dict) else text_token(v, []),
        })
    return {
        "Value": {"WFDictionaryFieldValueItems": items},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def action(identifier, params):
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": params,
    }


def build(base_url, api_key):
    base_url = base_url.rstrip("/")
    clipboard_uuid = new_uuid()
    resolve_uuid = new_uuid()
    url_value_uuid = new_uuid()
    media_uuid = new_uuid()

    # 1. Get Clipboard
    a1 = action("is.workflow.actions.getclipboard", {})

    # 2. POST {base}/resolve?key=... with JSON body {"url": "<clipboard>"}
    # WFJSONValues is a dictionary-field value: one item, key "url",
    # value = the clipboard token.
    body = dict_field_value([
        ("url", text_token("\ufffc", [
            (0, 1, clipboard_uuid, "Clipboard"),
        ])),
    ])
    a2 = action("is.workflow.actions.downloadurl", {
        "WFURL": text_token(f"{base_url}/resolve?key={api_key}", []),
        "WFHTTPMethod": "POST",
        "ShowHeaders": True,
        "WFHTTPBodyType": "JSON",
        "WFJSONValues": body,
        "WFHTTPHeaders": dict_field_value([
            ("Content-Type", "application/json"),
        ]),
    })

    # 3. Get dictionary value "url" (internal identifier is getvalueforkey,
    # not getdictionaryvalue)
    a3 = action("is.workflow.actions.getvalueforkey", {
        "WFDictionaryKey": "url",
        "WFGetDictionaryValueType": "Value",
    })

    # 4. Get Contents of the media URL (output of action 3)
    a4 = action("is.workflow.actions.downloadurl", {
        "WFURL": variable_value(url_value_uuid, "Dictionary Value"),
        "WFHTTPMethod": "GET",
    })

    # 5. Save to Photo Album (identifier is savetocameraroll, not savetophotoalbum)
    a5 = action("is.workflow.actions.savetocameraroll", {})

    # 6. Notification (identifier is notification, not shownotification)
    a6 = action("is.workflow.actions.notification", {
        "WFNotificationActionTitle": "下载视频",
        "WFNotificationActionBody": "视频已保存到相册",
        "WFNotificationActionSound": True,
    })

    actions = [a1, a2, a3, a4, a5, a6]
    # Attach output UUIDs so later actions can reference them, and give
    # every action its own UUID (Shortcuts import rejects actions without one).
    for act, out_uuid in [
        (a1, clipboard_uuid),
        (a2, resolve_uuid),
        (a3, url_value_uuid),
        (a4, media_uuid),
        (a5, new_uuid()),
        (a6, new_uuid()),
    ]:
        act["WFWorkflowActionParameters"]["UUID"] = out_uuid

    workflow = {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "1200",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": ["ActionExtension", "WatchKit"],
        "WFWorkflowInputContentItemClasses": [
            "WFURLContentItem",
            "WFStringContentItem",
        ],
        "WFWorkflowName": "下载视频",
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 1440408063,
            "WFWorkflowIconGlyphNumber": 59423,
        },
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
    }
    return workflow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    workflow = build(args.base_url, args.api_key)
    with open(args.out, "wb") as f:
        plistlib.dump(workflow, f, fmt=plistlib.FMT_BINARY)

    # Validate: reload and print the action list.
    with open(args.out, "rb") as f:
        check = plistlib.load(f)
    print("wrote:", args.out)
    print("name:", check["WFWorkflowName"])
    for act in check["WFWorkflowActions"]:
        print("-", act["WFWorkflowActionIdentifier"])


if __name__ == "__main__":
    main()
