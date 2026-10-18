
# --- failover front readiness (issue #296) ----------------------------------
# The spoke's /standby/local includes the failover front's /front/health only
# when this is set; the readiness panel then shows the front row. Derived by
# sync from the front's loopback bind. Edit freely — sync never overwrites an
# existing assignment, it only reports template drift.
export SNOWLINE_FRONT_HEALTH_URL="$front_health_url"
