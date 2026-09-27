def navigation(request):
    return {
        "nav_items": [
            ("dashboard", "Overview", "◈"),
            ("contacts", "Contacts", "◎"),
            ("organizations", "Organizations", "▦"),
            ("leads", "Leads", "↗"),
            ("deals", "Deals", "◇"),
            ("tasks", "Tasks", "✓"),
        ],
        "active_path": request.path,
    }
