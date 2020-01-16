import datetime

from ._core import log
from . import (
    _exception,
    _util,
    _graphql,
    _mqtt,
    _session,
    _poll,
    _user,
    _page,
    _group,
    _thread,
    _message,
    _event_common,
    _client_payload,
)

from ._thread import ThreadLocation
from ._user import User, UserData, ActiveStatus
from ._group import Group, GroupData
from ._page import Page, PageData
from ._message import EmojiSize, Mention, Message
from ._attachment import Attachment
from ._sticker import Sticker
from ._location import LocationAttachment, LiveLocationAttachment
from ._file import ImageAttachment, VideoAttachment
from ._quick_reply import (
    QuickReply,
    QuickReplyText,
    QuickReplyLocation,
    QuickReplyPhoneNumber,
    QuickReplyEmail,
)
from ._plan import PlanData

from typing import Sequence, Iterable, Tuple, Optional


class Client:
    """A client for the Facebook Chat (Messenger).

    This contains all the methods you use to interact with Facebook. You can extend this
    class, and overwrite the ``on`` methods, to provide custom event handling (mainly
    useful while listening).
    """

    def __init__(self, session):
        """Initialize the client model.

        Args:
            session: The session to use when making requests.
        """
        self._mark_alive = True
        self._buddylist = dict()
        self._session = session
        self._mqtt = None

    @property
    def session(self):
        """The session that's used when making requests."""
        return self._session

    def __repr__(self):
        return "Client(session={!r})".format(self._session)

    def fetch_users(self) -> Sequence[_user.UserData]:
        """Fetch users the client is currently chatting with.

        This is very close to your friend list, with the follow differences:

        It differs by including users that you're not friends with, but have chatted
        with before, and by including accounts that are "Messenger Only".

        But does not include deactivated, deleted or memorialized users (logically,
        since you can't chat with those).
        """
        data = {"viewer": self.session.user_id}
        j = self.session._payload_post("/chat/user_info_all", data)

        users = []
        for data in j.values():
            if data["type"] not in ["user", "friend"] or data["id"] in ["0", 0]:
                log.warning("Invalid user data %s", data)
                continue  # Skip invalid users
            users.append(_user.UserData._from_all_fetch(self.session, data))
        return users

    def search_for_users(self, name: str, limit: int) -> Iterable[_user.UserData]:
        """Find and get users by their name.

        Args:
            name: Name of the user
            limit: The max. amount of users to fetch

        Returns:
            list: `User` objects, ordered by relevance
        """
        params = {"search": name, "limit": limit}
        (j,) = self.session._graphql_requests(
            _graphql.from_query(_graphql.SEARCH_USER, params)
        )

        return (
            UserData._from_graphql(self.session, node)
            for node in j[name]["users"]["nodes"]
        )

    def search_for_pages(self, name: str, limit: int) -> Iterable[_page.PageData]:
        """Find and get pages by their name.

        Args:
            name: Name of the page
            limit: The max. amount of pages to fetch
        """
        params = {"search": name, "limit": limit}
        (j,) = self.session._graphql_requests(
            _graphql.from_query(_graphql.SEARCH_PAGE, params)
        )

        return (
            PageData._from_graphql(self.session, node)
            for node in j[name]["pages"]["nodes"]
        )

    def search_for_groups(self, name: str, limit: int) -> Iterable[_group.GroupData]:
        """Find and get group threads by their name.

        Args:
            name: Name of the group thread
            limit: The max. amount of groups to fetch
        """
        params = {"search": name, "limit": limit}
        (j,) = self.session._graphql_requests(
            _graphql.from_query(_graphql.SEARCH_GROUP, params)
        )

        return (
            GroupData._from_graphql(self.session, node)
            for node in j["viewer"]["groups"]["nodes"]
        )

    def search_for_threads(self, name: str, limit: int) -> Iterable[_thread.ThreadABC]:
        """Find and get threads by their name.

        Args:
            name: Name of the thread
            limit: The max. amount of threads to fetch
        """
        params = {"search": name, "limit": limit}
        (j,) = self.session._graphql_requests(
            _graphql.from_query(_graphql.SEARCH_THREAD, params)
        )

        for node in j[name]["threads"]["nodes"]:
            if node["__typename"] == "User":
                yield UserData._from_graphql(self.session, node)
            elif node["__typename"] == "MessageThread":
                # MessageThread => Group thread
                yield GroupData._from_graphql(self.session, node)
            elif node["__typename"] == "Page":
                yield PageData._from_graphql(self.session, node)
            elif node["__typename"] == "Group":
                # We don't handle Facebook "Groups"
                pass
            else:
                log.warning(
                    "Unknown type {} in {}".format(repr(node["__typename"]), node)
                )

    def _search_messages(self, query, offset, limit):
        data = {"query": query, "offset": offset, "limit": limit}
        j = self.session._payload_post("/ajax/mercury/search_snippets.php?dpr=1", data)

        total_snippets = j["search_snippets"][query]

        rtn = []
        for node in j["graphql_payload"]["message_threads"]:
            type_ = node["thread_type"]
            if type_ == "GROUP":
                thread = Group(
                    session=self.session, id=node["thread_key"]["thread_fbid"]
                )
            elif type_ == "ONE_TO_ONE":
                thread = _thread.Thread(
                    session=self.session, id=node["thread_key"]["other_user_id"]
                )
                # if True:  # TODO: This check!
                #     thread = UserData._from_graphql(self.session, node)
                # else:
                #     thread = PageData._from_graphql(self.session, node)
            else:
                thread = None
                log.warning("Unknown thread type %s, data: %s", type_, node)

            if thread:
                rtn.append((thread, total_snippets[thread.id]["num_total_snippets"]))
            else:
                rtn.append((None, 0))

        return rtn

    def search_messages(
        self, query: str, limit: Optional[int]
    ) -> Iterable[Tuple[_thread.ThreadABC, int]]:
        """Search for messages in all threads.

        Intended to be used alongside `ThreadABC.search_messages`

        Warning! If someone send a message to a thread that matches the query, while
        we're searching, some snippets will get returned twice.

        Not sure if we should handle it, Facebook's implementation doesn't...

        Args:
            query: Text to search for
            limit: Max. number of threads to retrieve. If ``None``, all threads will be
                retrieved.

        Returns:
            Iterable with tuples of threads, and the total amount of matches.
        """
        offset = 0
        # The max limit is measured empirically to ~500, safe default chosen below
        for limit in _util.get_limits(limit, max_limit=100):
            data = self._search_messages(query, offset, limit)
            for thread, total_snippets in data:
                if thread:
                    yield (thread, total_snippets)
            if len(data) < limit:
                return  # No more data to fetch
            offset += limit

    def _fetch_info(self, *ids):
        data = {"ids[{}]".format(i): _id for i, _id in enumerate(ids)}
        j = self.session._payload_post("/chat/user_info/", data)

        if j.get("profiles") is None:
            raise _exception.ParseError("No users/pages returned", data=j)

        entries = {}
        for _id in j["profiles"]:
            k = j["profiles"][_id]
            if k["type"] in ["user", "friend"]:
                entries[_id] = {
                    "id": _id,
                    "url": k.get("uri"),
                    "first_name": k.get("firstName"),
                    "is_viewer_friend": k.get("is_friend"),
                    "gender": k.get("gender"),
                    "profile_picture": {"uri": k.get("thumbSrc")},
                    "name": k.get("name"),
                }
            elif k["type"] == "page":
                entries[_id] = {
                    "id": _id,
                    "url": k.get("uri"),
                    "profile_picture": {"uri": k.get("thumbSrc")},
                    "name": k.get("name"),
                }
            else:
                raise _exception.ParseError("Unknown thread type", data=k)

        log.debug(entries)
        return entries

    def fetch_thread_info(self, *thread_ids):
        """Fetch threads' info from IDs, unordered.

        Warning:
            Sends two requests if users or pages are present, to fetch all available info!

        Args:
            thread_ids: One or more thread ID(s) to query

        Returns:
            dict: `Thread` objects, labeled by their ID
        """
        queries = []
        for thread_id in thread_ids:
            params = {
                "id": thread_id,
                "message_limit": 0,
                "load_messages": False,
                "load_read_receipts": False,
                "before": None,
            }
            queries.append(_graphql.from_doc_id("2147762685294928", params))

        j = self.session._graphql_requests(*queries)

        for i, entry in enumerate(j):
            if entry.get("message_thread") is None:
                # If you don't have an existing thread with this person, attempt to retrieve user data anyways
                j[i]["message_thread"] = {
                    "thread_key": {"other_user_id": thread_ids[i]},
                    "thread_type": "ONE_TO_ONE",
                }

        pages_and_user_ids = [
            k["message_thread"]["thread_key"]["other_user_id"]
            for k in j
            if k["message_thread"].get("thread_type") == "ONE_TO_ONE"
        ]
        pages_and_users = {}
        if len(pages_and_user_ids) != 0:
            pages_and_users = self._fetch_info(*pages_and_user_ids)

        rtn = {}
        for i, entry in enumerate(j):
            entry = entry["message_thread"]
            if entry.get("thread_type") == "GROUP":
                _id = entry["thread_key"]["thread_fbid"]
                rtn[_id] = GroupData._from_graphql(self.session, entry)
            elif entry.get("thread_type") == "ONE_TO_ONE":
                _id = entry["thread_key"]["other_user_id"]
                if pages_and_users.get(_id) is None:
                    raise _exception.ParseError(
                        "Could not fetch thread {}".format(_id), data=pages_and_users
                    )
                entry.update(pages_and_users[_id])
                if "first_name" in entry:
                    rtn[_id] = UserData._from_graphql(self.session, entry)
                else:
                    rtn[_id] = PageData._from_graphql(self.session, entry)
            else:
                raise _exception.ParseError("Unknown thread type", data=entry)

        return rtn

    def _fetch_threads(self, limit, before, folders):
        params = {
            "limit": limit,
            "tags": folders,
            "before": _util.datetime_to_millis(before) if before else None,
            "includeDeliveryReceipts": True,
            "includeSeqID": False,
        }
        (j,) = self.session._graphql_requests(
            _graphql.from_doc_id("1349387578499440", params)
        )

        rtn = []
        for node in j["viewer"]["message_threads"]["nodes"]:
            _type = node.get("thread_type")
            if _type == "GROUP":
                rtn.append(GroupData._from_graphql(self.session, node))
            elif _type == "ONE_TO_ONE":
                rtn.append(UserData._from_thread_fetch(self.session, node))
            else:
                rtn.append(None)
                log.warning("Unknown thread type: %s, data: %s", _type, node)
        return rtn

    def fetch_threads(
        self, limit: Optional[int], location: ThreadLocation = ThreadLocation.INBOX,
    ) -> Iterable[_thread.ThreadABC]:
        """Fetch the client's thread list.

        Args:
            limit: Max. number of threads to retrieve. If ``None``, all threads will be
                retrieved.
            location: INBOX, PENDING, ARCHIVED or OTHER
        """
        # This is measured empirically as 837, safe default chosen below
        MAX_BATCH_LIMIT = 100

        # TODO: Clean this up after implementing support for more threads types
        seen_ids = set()
        before = None
        for limit in _util.get_limits(limit, MAX_BATCH_LIMIT):
            threads = self._fetch_threads(limit, before, [location.value])

            before = None
            for thread in threads:
                # Don't return seen and unknown threads
                if thread and thread.id not in seen_ids:
                    seen_ids.add(thread.id)
                    # TODO: Ensure type-wise that .last_active is available
                    before = thread.last_active
                    yield thread

            if len(threads) < MAX_BATCH_LIMIT:
                return  # No more data to fetch

            # We check this here in case _fetch_threads only returned `None` threads
            if not before:
                raise ValueError("Too many unknown threads.")

    def fetch_unread(self):
        """Fetch unread threads.

        Returns:
            list: List of unread thread ids
        """
        form = {
            "folders[0]": "inbox",
            "client": "mercury",
            "last_action_timestamp": _util.now() - 60 * 1000
            # 'last_action_timestamp': 0
        }
        j = self.session._payload_post("/ajax/mercury/unread_threads.php", form)

        result = j["unread_thread_fbids"][0]
        return result["thread_fbids"] + result["other_user_fbids"]

    def fetch_unseen(self):
        """Fetch unseen / new threads.

        Returns:
            list: List of unseen thread ids
        """
        j = self.session._payload_post("/mercury/unseen_thread_ids/", {})

        result = j["unseen_thread_fbids"][0]
        return result["thread_fbids"] + result["other_user_fbids"]

    def fetch_image_url(self, image_id):
        """Fetch URL to download the original image from an image attachment ID.

        Args:
            image_id (str): The image you want to fetch

        Returns:
            str: An URL where you can download the original image
        """
        image_id = str(image_id)
        data = {"photo_id": str(image_id)}
        j = self.session._post("/mercury/attachments/photo/", data)
        _exception.handle_payload_error(j)

        url = _util.get_jsmods_require(j, 3)
        if url is None:
            raise _exception.ParseError("Could not fetch image URL", data=j)
        return url

    def _get_private_data(self):
        (j,) = self.session._graphql_requests(
            _graphql.from_doc_id("1868889766468115", {})
        )
        return j["viewer"]

    def get_phone_numbers(self):
        """Fetch list of user's phone numbers.

        Returns:
            list: List of phone numbers
        """
        data = self._get_private_data()
        return [
            j["phone_number"]["universal_number"] for j in data["user"]["all_phones"]
        ]

    def get_emails(self):
        """Fetch list of user's emails.

        Returns:
            list: List of emails
        """
        data = self._get_private_data()
        return [j["display_email"] for j in data["all_emails"]]

    def get_user_active_status(self, user_id):
        """Fetch friend active status as an `ActiveStatus` object.

        Return ``None`` if status isn't known.

        Warning:
            Only works when listening.

        Args:
            user_id: ID of the user

        Returns:
            ActiveStatus: Given user active status
        """
        return self._buddylist.get(str(user_id))

    def mark_as_delivered(self, thread_id, message_id):
        """Mark a message as delivered.

        Args:
            thread_id: User/Group ID to which the message belongs. See :ref:`intro_threads`
            message_id: Message ID to set as delivered. See :ref:`intro_threads`
        """
        data = {
            "message_ids[0]": message_id,
            "thread_ids[%s][0]" % thread_id: message_id,
        }

        j = self.session._payload_post("/ajax/mercury/delivery_receipts.php", data)
        return True

    def _read_status(self, read, thread_ids, timestamp=None):
        thread_ids = _util.require_list(thread_ids)

        data = {
            "watermarkTimestamp": _util.datetime_to_millis(timestamp)
            if timestamp
            else _util.now(),
            "shouldSendReadReceipt": "true",
        }

        for thread_id in thread_ids:
            data["ids[{}]".format(thread_id)] = "true" if read else "false"

        j = self.session._payload_post("/ajax/mercury/change_read_status.php", data)

    def mark_as_read(self, thread_ids=None, timestamp=None):
        """Mark threads as read.

        All messages inside the specified threads will be marked as read.

        Args:
            thread_ids: User/Group IDs to set as read. See :ref:`intro_threads`
            timestamp: Timestamp (as a Datetime) to signal the read cursor at, default is the current time
        """
        self._read_status(True, thread_ids, timestamp)

    def mark_as_unread(self, thread_ids=None, timestamp=None):
        """Mark threads as unread.

        All messages inside the specified threads will be marked as unread.

        Args:
            thread_ids: User/Group IDs to set as unread. See :ref:`intro_threads`
            timestamp: Timestamp (as a Datetime) to signal the read cursor at, default is the current time
        """
        self._read_status(False, thread_ids, timestamp)

    def mark_as_seen(self):
        """
        Todo:
            Documenting this
        """
        j = self.session._payload_post(
            "/ajax/mercury/mark_seen.php", {"seen_timestamp": _util.now()}
        )

    def move_threads(self, location, thread_ids):
        """Move threads to specified location.

        Args:
            location (ThreadLocation): INBOX, PENDING, ARCHIVED or OTHER
            thread_ids: Thread IDs to move. See :ref:`intro_threads`
        """
        thread_ids = _util.require_list(thread_ids)

        if location == ThreadLocation.PENDING:
            location = ThreadLocation.OTHER

        if location == ThreadLocation.ARCHIVED:
            data_archive = dict()
            data_unpin = dict()
            for thread_id in thread_ids:
                data_archive["ids[{}]".format(thread_id)] = "true"
                data_unpin["ids[{}]".format(thread_id)] = "false"
            j_archive = self.session._payload_post(
                "/ajax/mercury/change_archived_status.php?dpr=1", data_archive
            )
            j_unpin = self.session._payload_post(
                "/ajax/mercury/change_pinned_status.php?dpr=1", data_unpin
            )
        else:
            data = dict()
            for i, thread_id in enumerate(thread_ids):
                data["{}[{}]".format(location.name.lower(), i)] = thread_id
            j = self.session._payload_post("/ajax/mercury/move_thread.php", data)
        return True

    def delete_threads(self, thread_ids):
        """Delete threads.

        Args:
            thread_ids: Thread IDs to delete. See :ref:`intro_threads`
        """
        thread_ids = _util.require_list(thread_ids)

        data_unpin = dict()
        data_delete = dict()
        for i, thread_id in enumerate(thread_ids):
            data_unpin["ids[{}]".format(thread_id)] = "false"
            data_delete["ids[{}]".format(i)] = thread_id
        j_unpin = self.session._payload_post(
            "/ajax/mercury/change_pinned_status.php?dpr=1", data_unpin
        )
        j_delete = self.session._payload_post(
            "/ajax/mercury/delete_thread.php?dpr=1", data_delete
        )
        return True

    def delete_messages(self, message_ids):
        """Delete specified messages.

        Args:
            message_ids: Message IDs to delete
        """
        message_ids = _util.require_list(message_ids)
        data = dict()
        for i, message_id in enumerate(message_ids):
            data["message_ids[{}]".format(i)] = message_id
        j = self.session._payload_post("/ajax/mercury/delete_messages.php?dpr=1", data)
        return True

    """
    LISTEN METHODS
    """

    def _parse_delta(self, delta):
        def get_thread(data):
            if "threadFbId" in data["threadKey"]:
                group_id = str(data["threadKey"]["threadFbId"])
                return Group(session=self.session, id=group_id)
            elif "otherUserFbId" in data["threadKey"]:
                user_id = str(data["threadKey"]["otherUserFbId"])
                return User(session=self.session, id=user_id)
            return None

        delta_type = delta.get("type")
        delta_class = delta.get("class")
        metadata = delta.get("messageMetadata")

        if metadata:
            mid = metadata["messageId"]
            author_id = str(metadata["actorFbId"])
            at = _util.millis_to_datetime(int(metadata.get("timestamp")))

        # Added participants
        if "addedParticipants" in delta:
            added_ids = [str(x["userFbId"]) for x in delta["addedParticipants"]]
            self.on_people_added(
                mid=mid,
                added_ids=added_ids,
                author_id=author_id,
                group=get_thread(metadata),
                at=at,
            )

        # Left/removed participants
        elif "leftParticipantFbId" in delta:
            removed_id = str(delta["leftParticipantFbId"])
            self.on_person_removed(
                mid=mid,
                removed_id=removed_id,
                author_id=author_id,
                group=get_thread(metadata),
                at=at,
            )

        # Color change
        elif delta_type == "change_thread_theme":
            thread = get_thread(metadata)
            self.on_color_change(
                mid=mid,
                author_id=author_id,
                new_color=_thread.ThreadABC._parse_color(
                    delta["untypedData"]["theme_color"]
                ),
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        elif delta_class == "MarkFolderSeen":
            locations = [
                ThreadLocation(folder.lstrip("FOLDER_")) for folder in delta["folders"]
            ]
            at = _util.millis_to_datetime(int(delta["timestamp"]))
            self._on_seen(locations=locations, at=at)

        # Emoji change
        elif delta_type == "change_thread_icon":
            new_emoji = delta["untypedData"]["thread_icon"]
            self.on_emoji_change(
                mid=mid,
                author_id=author_id,
                new_emoji=new_emoji,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Thread title change
        elif delta_class == "ThreadName":
            new_title = delta["name"]
            self.on_title_change(
                mid=mid,
                author_id=author_id,
                new_title=new_title,
                group=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Forced fetch
        elif delta_class == "ForcedFetch":
            mid = delta.get("messageId")
            if mid is None:
                self.on_unknown_messsage_type(msg=delta)
            else:
                group = get_thread(delta)
                fetch_info = group._forced_fetch(mid)
                fetch_data = fetch_info["message"]
                author_id = fetch_data["message_sender"]["id"]
                at = _util.millis_to_datetime(int(fetch_data["timestamp_precise"]))
                if fetch_data.get("__typename") == "ThreadImageMessage":
                    # Thread image change
                    image_metadata = fetch_data.get("image_with_metadata")
                    image_id = (
                        int(image_metadata["legacy_attachment_id"])
                        if image_metadata
                        else None
                    )
                    self.on_image_change(
                        mid=mid,
                        author_id=author_id,
                        new_image=image_id,
                        group=group,
                        at=at,
                    )

        # Nickname change
        elif delta_type == "change_thread_nickname":
            changed_for = str(delta["untypedData"]["participant_id"])
            new_nickname = delta["untypedData"]["nickname"]
            self.on_nickname_change(
                mid=mid,
                author_id=author_id,
                changed_for=changed_for,
                new_nickname=new_nickname,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Admin added or removed in a group thread
        elif delta_type == "change_thread_admins":
            target_id = delta["untypedData"]["TARGET_ID"]
            admin_event = delta["untypedData"]["ADMIN_EVENT"]
            if admin_event == "add_admin":
                self.on_admin_added(
                    mid=mid,
                    added_id=target_id,
                    author_id=author_id,
                    group=get_thread(metadata),
                    at=at,
                )
            elif admin_event == "remove_admin":
                self.on_admin_removed(
                    mid=mid,
                    removed_id=target_id,
                    author_id=author_id,
                    group=get_thread(metadata),
                    at=at,
                )

        # Group approval mode change
        elif delta_type == "change_thread_approval_mode":
            approval_mode = bool(int(delta["untypedData"]["APPROVAL_MODE"]))
            self.on_approval_mode_change(
                mid=mid,
                approval_mode=approval_mode,
                author_id=author_id,
                group=get_thread(metadata),
                at=at,
            )

        # Message delivered
        elif delta_class == "DeliveryReceipt":
            message_ids = delta["messageIds"]
            delivered_for = str(
                delta.get("actorFbId") or delta["threadKey"]["otherUserFbId"]
            )
            at = _util.millis_to_datetime(int(delta["deliveredWatermarkTimestampMs"]))
            self.on_message_delivered(
                msg_ids=message_ids,
                delivered_for=delivered_for,
                thread=get_thread(delta),
                at=at,
                metadata=metadata,
            )

        # Message seen
        elif delta_class == "ReadReceipt":
            seen_by = str(delta.get("actorFbId") or delta["threadKey"]["otherUserFbId"])
            seen_at = _util.millis_to_datetime(int(delta["actionTimestampMs"]))
            at = _util.millis_to_datetime(int(delta["watermarkTimestampMs"]))
            self.on_message_seen(
                seen_by=seen_by,
                thread=get_thread(delta),
                seen_at=seen_at,
                at=at,
                metadata=metadata,
            )

        # Messages marked as seen
        elif delta_class == "MarkRead":
            seen_at = _util.millis_to_datetime(
                int(delta.get("actionTimestampMs") or delta.get("actionTimestamp"))
            )
            watermark_ts = delta.get("watermarkTimestampMs") or delta.get(
                "watermarkTimestamp"
            )
            at = _util.millis_to_datetime(int(watermark_ts))

            threads = []
            if "folders" not in delta:
                threads = [
                    get_thread({"threadKey": thr}) for thr in delta.get("threadKeys")
                ]

            self.on_marked_seen(threads=threads, seen_at=seen_at, at=at, metadata=delta)

        # Game played
        elif delta_type == "instant_game_update":
            game_id = delta["untypedData"]["game_id"]
            game_name = delta["untypedData"]["game_name"]
            score = delta["untypedData"].get("score")
            if score is not None:
                score = int(score)
            leaderboard = delta["untypedData"].get("leaderboard")
            if leaderboard is not None:
                leaderboard = _util.parse_json(leaderboard)["scores"]
            self.on_game_played(
                mid=mid,
                author_id=author_id,
                game_id=game_id,
                game_name=game_name,
                score=score,
                leaderboard=leaderboard,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Skip "no operation" events
        elif delta_class == "NoOp":
            pass

        # Group call started/ended
        elif delta_type == "rtc_call_log":
            call_status = delta["untypedData"]["event"]
            call_duration = _util.seconds_to_timedelta(
                int(delta["untypedData"]["call_duration"])
            )
            is_video_call = bool(int(delta["untypedData"]["is_video_call"]))
            if call_status == "call_started":
                self.on_call_started(
                    mid=mid,
                    caller_id=author_id,
                    is_video_call=is_video_call,
                    thread=get_thread(metadata),
                    at=at,
                    metadata=metadata,
                )
            elif call_status == "call_ended":
                self.on_call_ended(
                    mid=mid,
                    caller_id=author_id,
                    is_video_call=is_video_call,
                    call_duration=call_duration,
                    thread=get_thread(metadata),
                    at=at,
                    metadata=metadata,
                )

        # User joined to group call
        elif delta_type == "participant_joined_group_call":
            is_video_call = bool(int(delta["untypedData"]["group_call_type"]))
            self.on_user_joined_call(
                mid=mid,
                joined_id=author_id,
                is_video_call=is_video_call,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Group poll event
        elif delta_type == "group_poll":
            event_type = delta["untypedData"]["event_type"]
            poll_json = _util.parse_json(delta["untypedData"]["question_json"])
            poll = _poll.Poll._from_graphql(self.session, poll_json)
            if event_type == "question_creation":
                # User created group poll
                self.on_poll_created(
                    mid=mid,
                    poll=poll,
                    author_id=author_id,
                    thread=get_thread(metadata),
                    at=at,
                    metadata=metadata,
                )
            elif event_type == "update_vote":
                # User voted on group poll
                added = _util.parse_json(delta["untypedData"]["added_option_ids"])
                removed = _util.parse_json(delta["untypedData"]["removed_option_ids"])
                self.on_poll_voted(
                    mid=mid,
                    poll=poll,
                    added_options=added,
                    removed_options=removed,
                    author_id=author_id,
                    thread=get_thread(metadata),
                    at=at,
                    metadata=metadata,
                )

        # Plan created
        elif delta_type == "lightweight_event_create":
            self.on_plan_created(
                mid=mid,
                plan=PlanData._from_pull(self.session, delta["untypedData"]),
                author_id=author_id,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Plan ended
        elif delta_type == "lightweight_event_notify":
            self.on_plan_ended(
                mid=mid,
                plan=PlanData._from_pull(self.session, delta["untypedData"]),
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Plan edited
        elif delta_type == "lightweight_event_update":
            self.on_plan_edited(
                mid=mid,
                plan=PlanData._from_pull(self.session, delta["untypedData"]),
                author_id=author_id,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Plan deleted
        elif delta_type == "lightweight_event_delete":
            self.on_plan_deleted(
                mid=mid,
                plan=PlanData._from_pull(self.session, delta["untypedData"]),
                author_id=author_id,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Plan participation change
        elif delta_type == "lightweight_event_rsvp":
            take_part = delta["untypedData"]["guest_status"] == "GOING"
            self.on_plan_participation(
                mid=mid,
                plan=PlanData._from_pull(self.session, delta["untypedData"]),
                take_part=take_part,
                author_id=author_id,
                thread=get_thread(metadata),
                at=at,
                metadata=metadata,
            )

        # Client payload (that weird numbers)
        elif delta_class == "ClientPayload":
            for event in _client_payload.parse_client_payloads(self.session, delta):
                self.on_event(event)

        # New message
        elif delta.get("class") == "NewMessage":
            thread = get_thread(metadata)
            self.on_message(
                mid=mid,
                author_id=author_id,
                message_object=_message.MessageData._from_pull(
                    thread,
                    delta,
                    mid=mid,
                    tags=metadata.get("tags"),
                    author=author_id,
                    created_at=at,
                ),
                thread=thread,
                at=at,
                metadata=metadata,
            )

        # Unknown message type
        else:
            self.on_unknown_messsage_type(msg=delta)

    def _parse_payload(self, topic, m):
        # Things that directly change chat
        if topic == "/t_ms":
            if "deltas" not in m:
                return
            for delta in m["deltas"]:
                self._parse_delta(delta)

        # TODO: Remove old parsing below

        # Inbox
        elif topic == "inbox":
            self.on_inbox(
                unseen=m["unseen"],
                unread=m["unread"],
                recent_unread=m["recent_unread"],
            )

        # Typing
        # /thread_typing {'sender_fbid': X, 'state': 1, 'type': 'typ', 'thread': 'Y'}
        # /orca_typing_notifications {'type': 'typ', 'sender_fbid': X, 'state': 0}
        elif topic in ("/thread_typing", "/orca_typing_notifications"):
            author_id = str(m["sender_fbid"])
            thread_id = m.get("thread")
            if thread_id:
                thread = _group.Group(session=self.session, id=str(thread_id))
            else:
                thread = _user.User(session=self.session, id=author_id)
            self.on_typing(
                author_id=author_id, status=m["state"] == 1, thread=thread,
            )

        # Other notifications
        elif topic == "/legacy_web":
            # Friend request
            if m["type"] == "jewel_requests_add":
                self.on_friend_request(from_id=str(m["from"]))
            else:
                self.on_unknown_messsage_type(msg=m)

        # Chat timestamp / Buddylist overlay
        elif topic == "/orca_presence":
            if m["list_type"] == "full":
                self._buddylist = {}  # Refresh internal list

            statuses = dict()
            for data in m["list"]:
                user_id = str(data["u"])
                statuses[user_id] = ActiveStatus._from_orca_presence(data)
                self._buddylist[user_id] = statuses[user_id]

            # TODO: Which one should we call?
            self.on_chat_timestamp(buddylist=statuses)
            self.on_buddylist_overlay(statuses=statuses)

        # Unknown message type
        else:
            self.on_unknown_messsage_type(msg=m)

    def _parse_message(self, topic, data):
        try:
            self._parse_payload(topic, data)
        except Exception as e:
            self.on_message_error(exception=e, msg=data)

    def _start_listening(self):
        if not self._mqtt:
            self._mqtt = _mqtt.Mqtt.connect(
                session=self.session,
                on_message=self._parse_message,
                chat_on=self._mark_alive,
                foreground=True,
            )

    def _do_one_listen(self):
        # TODO: Remove this wierd check, and let the user handle the chat_on parameter
        if self._mark_alive != self._mqtt._chat_on:
            self._mqtt.set_chat_on(self._mark_alive)

        return self._mqtt.loop_once()

    def _stop_listening(self):
        if not self._mqtt:
            return
        self._mqtt.disconnect()
        # TODO: Preserve the _mqtt object
        # Currently, there's some issues when disconnecting
        self._mqtt = None

    def listen(self, markAlive=None):
        """Initialize and runs the listening loop continually.

        Args:
            markAlive (bool): Whether this should ping the Facebook server each time the loop runs
        """
        if markAlive is not None:
            self.set_active_status(markAlive)

        self._start_listening()

        while self._do_one_listen():
            pass

        self._stop_listening()

    def set_active_status(self, markAlive):
        """Change active status while listening.

        Args:
            markAlive (bool): Whether to show if client is active
        """
        self._mark_alive = markAlive

    """
    END LISTEN METHODS
    """

    """
    EVENTS
    """

    def on_event(self, event: _event_common.Event):
        """Called when the client is listening, and an event happens."""
        log.info("Got event: %s", event)

    def on_message(
        self,
        mid=None,
        author_id=None,
        message_object=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody sends a message.

        Args:
            mid: The message ID
            author_id: The ID of the author
            message_object (Message): The message (As a `Message` object)
            thread: Thread that the message was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the message was sent
            metadata: Extra metadata about the message
        """
        log.info("{} from {} in {}".format(message_object, author_id, thread))

    def on_color_change(
        self,
        mid=None,
        author_id=None,
        new_color=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody changes a thread's color.

        Args:
            mid: The action ID
            author_id: The ID of the person who changed the color
            new_color: The new color. Not limited to the ones in `ThreadABC.set_color`
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("Color change from {} in {}: {}".format(author_id, thread, new_color))

    def on_emoji_change(
        self,
        mid=None,
        author_id=None,
        new_emoji=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody changes a thread's emoji.

        Args:
            mid: The action ID
            author_id: The ID of the person who changed the emoji
            new_emoji: The new emoji
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("Emoji change from {} in {}: {}".format(author_id, thread, new_emoji))

    def on_title_change(
        self,
        mid=None,
        author_id=None,
        new_title=None,
        group=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody changes a thread's title.

        Args:
            mid: The action ID
            author_id: The ID of the person who changed the title
            new_title: The new title
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("Title change from {} in {}: {}".format(author_id, group, new_title))

    def on_image_change(
        self, mid=None, author_id=None, new_image=None, group=None, at=None
    ):
        """Called when the client is listening, and somebody changes a thread's image.

        Args:
            mid: The action ID
            author_id: The ID of the person who changed the image
            new_image: The ID of the new image
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
        """
        log.info("{} changed group image in {}".format(author_id, group))

    def on_nickname_change(
        self,
        mid=None,
        author_id=None,
        changed_for=None,
        new_nickname=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody changes a nickname.

        Args:
            mid: The action ID
            author_id: The ID of the person who changed the nickname
            changed_for: The ID of the person whom got their nickname changed
            new_nickname: The new nickname
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info(
            "Nickname change from {} in {} for {}: {}".format(
                author_id, thread, changed_for, new_nickname
            )
        )

    def on_admin_added(
        self, mid=None, added_id=None, author_id=None, group=None, at=None
    ):
        """Called when the client is listening, and somebody adds an admin to a group.

        Args:
            mid: The action ID
            added_id: The ID of the admin who got added
            author_id: The ID of the person who added the admins
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
        """
        log.info("{} added admin: {} in {}".format(author_id, added_id, group))

    def on_admin_removed(
        self, mid=None, removed_id=None, author_id=None, group=None, at=None
    ):
        """Called when the client is listening, and somebody is removed as an admin in a group.

        Args:
            mid: The action ID
            removed_id: The ID of the admin who got removed
            author_id: The ID of the person who removed the admins
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
        """
        log.info("{} removed admin: {} in {}".format(author_id, removed_id, group))

    def on_approval_mode_change(
        self, mid=None, approval_mode=None, author_id=None, group=None, at=None,
    ):
        """Called when the client is listening, and somebody changes approval mode in a group.

        Args:
            mid: The action ID
            approval_mode: True if approval mode is activated
            author_id: The ID of the person who changed approval mode
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
        """
        if approval_mode:
            log.info("{} activated approval mode in {}".format(author_id, group))
        else:
            log.info("{} disabled approval mode in {}".format(author_id, group))

    def on_message_seen(
        self, seen_by=None, thread=None, seen_at=None, at=None, metadata=None
    ):
        """Called when the client is listening, and somebody marks a message as seen.

        Args:
            seen_by: The ID of the person who marked the message as seen
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            seen_at (datetime.datetime): When the person saw the message
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("Messages seen by {} in {} at {}".format(seen_by, thread, seen_at))

    def on_message_delivered(
        self, msg_ids=None, delivered_for=None, thread=None, at=None, metadata=None,
    ):
        """Called when the client is listening, and somebody marks messages as delivered.

        Args:
            msg_ids: The messages that are marked as delivered
            delivered_for: The person that marked the messages as delivered
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info(
            "Messages {} delivered to {} in {} at {}".format(
                msg_ids, delivered_for, thread, at
            )
        )

    def on_marked_seen(self, threads=None, seen_at=None, at=None, metadata=None):
        """Called when the client is listening, and the client has successfully marked threads as seen.

        Args:
            threads: The threads that were marked
            author_id: The ID of the person who changed the emoji
            seen_at (datetime.datetime): When the threads were seen
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("Marked messages as seen in threads {} at {}".format(threads, seen_at))

    def on_people_added(
        self, mid=None, added_ids=None, author_id=None, group=None, at=None
    ):
        """Called when the client is listening, and somebody adds people to a group thread.

        Args:
            mid: The action ID
            added_ids: The IDs of the people who got added
            author_id: The ID of the person who added the people
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
        """
        log.info("{} added: {} in {}".format(author_id, ", ".join(added_ids), group))

    def on_person_removed(
        self, mid=None, removed_id=None, author_id=None, group=None, at=None
    ):
        """Called when the client is listening, and somebody removes a person from a group thread.

        Args:
            mid: The action ID
            removed_id: The ID of the person who got removed
            author_id: The ID of the person who removed the person
            group: Group that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
        """
        log.info("{} removed: {} in {}".format(author_id, removed_id, group))

    def on_friend_request(self, from_id=None):
        """Called when the client is listening, and somebody sends a friend request.

        Args:
            from_id: The ID of the person that sent the request
        """
        log.info("Friend request from {}".format(from_id))

    def _on_seen(self, locations=None, at=None):
        """
        Todo:
            Document this, and make it public

        Args:
            locations: ---
            at: A timestamp of the action
        """

    def on_inbox(self, unseen=None, unread=None, recent_unread=None):
        """
        Todo:
            Documenting this

        Args:
            unseen: --
            unread: --
            recent_unread: --
        """
        log.info("Inbox event: {}, {}, {}".format(unseen, unread, recent_unread))

    def on_typing(self, author_id=None, status=None, thread=None):
        """Called when the client is listening, and somebody starts or stops typing into a chat.

        Args:
            author_id: The ID of the person who sent the action
            is_typing: ``True`` if the user started typing, ``False`` if they stopped.
            thread: Thread that the action was sent to. See :ref:`intro_threads`
        """
        pass

    def on_game_played(
        self,
        mid=None,
        author_id=None,
        game_id=None,
        game_name=None,
        score=None,
        leaderboard=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody plays a game.

        Args:
            mid: The action ID
            author_id: The ID of the person who played the game
            game_id: The ID of the game
            game_name: Name of the game
            score: Score obtained in the game
            leaderboard: Actual leader board of the game in the thread
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info('{} played "{}" in {}'.format(author_id, game_name, thread))

    def on_call_started(
        self,
        mid=None,
        caller_id=None,
        is_video_call=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody starts a call in a group.

        Todo:
            Make this work with private calls.

        Args:
            mid: The action ID
            caller_id: The ID of the person who started the call
            is_video_call: True if it's video call
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} started call in {}".format(caller_id, thread))

    def on_call_ended(
        self,
        mid=None,
        caller_id=None,
        is_video_call=None,
        call_duration=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody ends a call in a group.

        Todo:
            Make this work with private calls.

        Args:
            mid: The action ID
            caller_id: The ID of the person who ended the call
            is_video_call: True if it was video call
            call_duration (datetime.timedelta): Call duration
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} ended call in {}".format(caller_id, thread))

    def on_user_joined_call(
        self,
        mid=None,
        joined_id=None,
        is_video_call=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody joins a group call.

        Args:
            mid: The action ID
            joined_id: The ID of the person who joined the call
            is_video_call: True if it's video call
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} joined call in {}".format(joined_id, thread))

    def on_poll_created(
        self, mid=None, poll=None, author_id=None, thread=None, at=None, metadata=None,
    ):
        """Called when the client is listening, and somebody creates a group poll.

        Args:
            mid: The action ID
            poll (Poll): Created poll
            author_id: The ID of the person who created the poll
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} created poll {} in {}".format(author_id, poll, thread))

    def on_poll_voted(
        self,
        mid=None,
        poll=None,
        added_options=None,
        removed_options=None,
        author_id=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody votes in a group poll.

        Args:
            mid: The action ID
            poll (Poll): Poll, that user voted in
            author_id: The ID of the person who voted in the poll
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} voted in poll {} in {}".format(author_id, poll, thread))

    def on_plan_created(
        self, mid=None, plan=None, author_id=None, thread=None, at=None, metadata=None,
    ):
        """Called when the client is listening, and somebody creates a plan.

        Args:
            mid: The action ID
            plan (Plan): Created plan
            author_id: The ID of the person who created the plan
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} created plan {} in {}".format(author_id, plan, thread))

    def on_plan_ended(self, mid=None, plan=None, thread=None, at=None, metadata=None):
        """Called when the client is listening, and a plan ends.

        Args:
            mid: The action ID
            plan (Plan): Ended plan
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("Plan {} has ended in {}".format(plan, thread))

    def on_plan_edited(
        self, mid=None, plan=None, author_id=None, thread=None, at=None, metadata=None,
    ):
        """Called when the client is listening, and somebody edits a plan.

        Args:
            mid: The action ID
            plan (Plan): Edited plan
            author_id: The ID of the person who edited the plan
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} edited plan {} in {}".format(author_id, plan, thread))

    def on_plan_deleted(
        self, mid=None, plan=None, author_id=None, thread=None, at=None, metadata=None,
    ):
        """Called when the client is listening, and somebody deletes a plan.

        Args:
            mid: The action ID
            plan (Plan): Deleted plan
            author_id: The ID of the person who deleted the plan
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        log.info("{} deleted plan {} in {}".format(author_id, plan, thread))

    def on_plan_participation(
        self,
        mid=None,
        plan=None,
        take_part=None,
        author_id=None,
        thread=None,
        at=None,
        metadata=None,
    ):
        """Called when the client is listening, and somebody takes part in a plan or not.

        Args:
            mid: The action ID
            plan (Plan): Plan
            take_part (bool): Whether the person takes part in the plan or not
            author_id: The ID of the person who will participate in the plan or not
            thread: Thread that the action was sent to. See :ref:`intro_threads`
            at (datetime.datetime): When the action was executed
            metadata: Extra metadata about the action
        """
        if take_part:
            log.info(
                "{} will take part in {} in {} ({})".format(author_id, plan, thread)
            )
        else:
            log.info(
                "{} won't take part in {} in {} ({})".format(author_id, plan, thread)
            )

    def on_chat_timestamp(self, buddylist=None):
        """Called when the client receives chat online presence update.

        Args:
            buddylist: A list of dictionaries with friend id and last seen timestamp
        """
        log.debug("Chat Timestamps received: {}".format(buddylist))

    def on_buddylist_overlay(self, statuses=None):
        """Called when the client is listening and client receives information about friend active status.

        Args:
            statuses (dict): Dictionary with user IDs as keys and `ActiveStatus` as values
        """

    def on_unknown_messsage_type(self, msg=None):
        """Called when the client is listening, and some unknown data was received.

        Args:
        """
        log.debug("Unknown message received: {}".format(msg))

    def on_message_error(self, exception=None, msg=None):
        """Called when an error was encountered while parsing received data.

        Args:
            exception: The exception that was encountered
        """
        log.exception("Exception in parsing of {}".format(msg))

    """
    END EVENTS
    """
