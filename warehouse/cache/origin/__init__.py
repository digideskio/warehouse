# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import functools

import jinja2

from pyramid.request import Request
from pyramid.threadlocal import get_current_request

from warehouse import db
from warehouse.cache.origin.interfaces import IOriginCache
from warehouse.cache.http import add_vary_callback


@jinja2.contextfunction
def esi_include(ctx, path, cookies=False):
    request = ctx.get("request") or get_current_request()

    if request.registry.settings.get("warehouse.prevent_esi", False):
        return ""

    try:
        cacher = request.find_service(IOriginCache)
    except ValueError:
        subreq = Request.blank(path)
        if cookies:
            subreq.cookies.update(request.cookies)
            request.add_response_callback(add_vary_callback("Cookie"))
        resp = request.invoke_subrequest(subreq, use_tweens=True)
        include = resp.body.decode(resp.charset)
    else:
        include = cacher.esi_include(request, path, cookies=cookies)

    return jinja2.Markup(include)


@db.listens_for(db.Session, "after_flush")
def store_purge_keys(config, session, flush_context):
    cache_keys = config.registry["cache_keys"]

    # We'll (ab)use the session.info dictionary to store a list of pending
    # purges to the session.
    purges = session.info.setdefault("warehouse.cache.origin.purges", set())

    # Go through each new, changed, and deleted object and attempt to store
    # a cache key that we'll want to purge when the session has been committed.
    for obj in (session.new | session.dirty | session.deleted):
        try:
            key_maker = cache_keys[obj.__class__]
        except KeyError:
            continue

        purges.update(key_maker(obj).purge)


@db.listens_for(db.Session, "after_commit")
def execute_purge(config, session):
    purges = session.info.pop("warehouse.cache.origin.purges", set())

    try:
        cacher_factory = config.find_service_factory(IOriginCache)
    except ValueError:
        return

    cacher = cacher_factory(None, config)
    cacher.purge(purges)


def origin_cache(seconds, keys=None, stale_while_revalidate=None,
                 stale_if_error=None):
    if keys is None:
        keys = []

    def inner(view):
        @functools.wraps(view)
        def wrapped(context, request):
            cache_keys = request.registry["cache_keys"]

            context_keys = []
            if context.__class__ in cache_keys:
                context_keys = cache_keys[context.__class__](context).cache

            try:
                cacher = request.find_service(IOriginCache)
            except ValueError:
                pass
            else:
                request.add_response_callback(
                    functools.partial(
                        cacher.cache,
                        sorted(context_keys + keys),
                        seconds=seconds,
                        stale_while_revalidate=stale_while_revalidate,
                        stale_if_error=stale_if_error,
                    )
                )

            return view(context, request)
        return wrapped

    return inner


CacheKeys = collections.namedtuple("CacheKeys", ["cache", "purge"])


def key_maker_factory(cache_keys, purge_keys):
    if cache_keys is None:
        cache_keys = []

    if purge_keys is None:
        purge_keys = []

    def key_maker(obj):
        return CacheKeys(
            cache=[k.format(obj=obj) for k in cache_keys],
            purge=[k.format(obj=obj) for k in purge_keys],
        )

    return key_maker


def register_origin_cache_keys(config, klass, cache_keys=None,
                               purge_keys=None):
    key_makers = config.registry.setdefault("cache_keys", {})
    key_makers[klass] = key_maker_factory(
        cache_keys=cache_keys,
        purge_keys=purge_keys,
    )


def includeme(config):
    if "origin_cache.backend" in config.registry.settings:
        cache_class = config.maybe_dotted(
            config.registry.settings["origin_cache.backend"],
        )
        config.register_service_factory(
            cache_class.create_service,
            IOriginCache,
        )

    config.add_directive(
        "register_origin_cache_keys",
        register_origin_cache_keys,
    )
