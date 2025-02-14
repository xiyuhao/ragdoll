"""Ragdoll Licence API"""

from maya import cmds
import logging

STATUS_OK = 0                  # All OK
STATUS_FAIL = 1                # General error
STATUS_ACTIVATE = 3            # The product needs to be activated.
STATUS_ALREADY_ACTIVATED = 26  # No action needed
STATUS_INET = 4                # Connection to the server failed.
STATUS_INET_DELAYED = 21       # Waiting 5 hours to reconnect with server
STATUS_INUSE = 5               # Maximum number of activations reached
STATUS_TRIAL_EXPIRED = 30      # Maximum number of activations reached

log = logging.getLogger("ragdoll")


def install(key=None):
    """Initialise licence mechanism

    This must be called prior to calling anything licence related.

    Arguments:
        key (str, optional): Automatically activate upon install

    """

    status = cmds.ragdollLicence(init=True)

    if status == STATUS_OK:
        log.debug("Successfully initialised Ragdoll licence.")

    elif status == STATUS_INET or status == STATUS_INET_DELAYED:
        log.warning(
            "Ragdoll is activated, but failed to verify the activation\n"
            "with the licence servers. You can still use the app for the\n"
            "duration of the grace period."
        )

    elif status == STATUS_ACTIVATE:
        log.warning(
            "Ragdoll is registered, but needs to reconnect with the\n"
            "licence server before continuing. Make sure your computer\n"
            "is connected to the internet, and try again."
        )

    elif status == STATUS_FAIL:
        log.warning(
            "Couldn't figure out licencing, "
            "this is a bug. Tell someone."
        )

    elif status == STATUS_TRIAL_EXPIRED:
        log.warning(
            "Ragdoll trial has expired, head into the chat and tell someone!"
        )

    else:
        log.error(
            "Unexpected error occurred with licencing, "
            "this is a bug. Tell someone."
        )

    if key is not None and not cmds.ragdollLicence(isActivated=True):
        log.info("Automatically activating Ragdoll licence")
        return activate(key)

    return status


def current_key():
    """Return the currently activated key, if any"""
    return cmds.ragdollLicence(serial=True)


def activate(key):
    """Register your key with the Ragdoll licence server

    Provide your key here to win a prize, the prize of being
    able to use Ragdoll forever and ever!

    """

    status = cmds.ragdollLicence(activate=key)

    if status == STATUS_OK:
        log.info("Successfully activated your Ragdoll licence.")

    elif status == STATUS_FAIL:
        log.error("Failed to activate licence, check your product key.")

    elif status == STATUS_ALREADY_ACTIVATED:
        log.error(
            "Already activated. To activate with a new "
            "key, call deactivate() first."
        )

    elif status == STATUS_INET:
        log.error(
            "An internet connection is required to activate."
        )

    elif status == STATUS_INUSE:
        log.error(
            "Maximum number of activations used.\n"
            "Try deactivating any previously activated licence.\n"
            "If you can no longer access the previously activated "
            "licence, contact licencing@ragdolldynamics.com for "
            "manual activation.")

    else:
        log.error("Unknown error (%d) occurred, this is a bug." % status)

    return status


def deactivate():
    """Release currently activated key from this machine

    Moving to another machine? Call this to enable activation
    on another machine.

    """

    status = cmds.ragdollLicence(deactivate=True)

    if status == STATUS_OK:
        log.info("Successfully deactivated Ragdoll licence.")

    elif status == STATUS_TRIAL_EXPIRED:
        log.info("Successfully deactivated Ragdoll licence, "
                 "but your trial has expired.")

    else:
        log.error("Couldn't deactivate Ragdoll licence "
                  "(error code: %s)." % status)

    return status


def reverify():
    return cmds.ragdollLicence(reverify=True)


def data():
    """Return overall information about the current Ragdoll licence"""
    return dict(
        key=cmds.ragdollLicence(serial=True, query=True),

        # Which edition of Ragdoll is this?
        # Standard or Enterprise
        edition="Enterprise",

        # Node-locked or floating
        floating=False,

        # Is the current licence activated?
        isActivated=cmds.ragdollLicence(isActivated=True, query=True),

        # Is the current licence a trial licence?
        isTrial=cmds.ragdollLicence(isTrial=True, query=True),

        # Has the licence not been tampered with?
        isGenuine=cmds.ragdollLicence(isGenuine=True, query=True),

        # Has the licence been verified with the server
        # (requires a connection to the internet)?
        isVerified=cmds.ragdollLicence(isVerified=True, query=True),

        # How many days until this trial expires?
        trialDays=cmds.ragdollLicence(trialDays=True, query=True),

        # How many magic days are left?
        magicDays=cmds.ragdollLicence(magicDays=True, query=True),
    )
