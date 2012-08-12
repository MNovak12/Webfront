from django import template
from django.conf import settings

import calendar
import datetime
import pytz

register = template.Library()


@register.filter
def date_presenter(datet):
    """Decides whether to render a datetime as H:M or b d, based on today's date.

    Because this is a filter, we need to muck around with timezones /sadface.
    """
    now = datetime.datetime.now(pytz.utc)
    print now.__repr__()

    if datet - now < datetime.timedelta(days=1):
        return datet.astimezone(pytz.timezone(settings.TIME_ZONE)).strftime("%H:%M")

    return datet.astimezone(pytz.timezone(settings.TIME_ZONE)).strftime("%b %d")


@register.inclusion_tag('events/calendar.html')
def calendarize(dt_one, dt_two=None, mark_today=False):
    """Renders a pretty calendar for a datetime's month.

    Because we access the attributes of the datetimes directly, we have
    to manually cast them into the timezone we want (otherwise we'd get utc
    days/times, which probably aren't correct for settings.TIMEZONE.)
    """
    dt_one = dt_one.astimezone(pytz.timezone(settings.TIME_ZONE))
    matrix = calendar.monthcalendar(dt_one.year, dt_one.month)

    if dt_two is None:
        dt_two = dt_one
    else:
        dt_two = dt_two.astimezone(pytz.timezone(settings.TIME_ZONE))

    if mark_today:
        today = datetime.datetime.now(pytz.utc)
    else:
        today = None

    return {
        'start': dt_one,
        'end': dt_two,
        'matrix': matrix,
        'today': today,
    }
