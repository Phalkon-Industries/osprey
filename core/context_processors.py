from django.conf import settings


def osprey_instance(request):
    """Expose instance-level flags (e.g. sandbox banner) to templates."""
    return {
        "osprey_is_sandbox": getattr(settings, "OSPREY_IS_SANDBOX", False),
    }
