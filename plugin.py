    async def _cleanup_pending_slot_messages(self):
        """
        Discard pending buffered slot messages that have been waiting too long (default: >5 seconds).
        This prevents memory leaks/race; warn for any lost messages.
        """
        now = time.time()
        TIMEOUT = 5.0
        stale_slots = []
        for idx, lst in self._pending_slot_messages.items():
            still_valid = []
            for ts, msg in lst:
                if now - ts > TIMEOUT:
                    self.logger.warning(
                        "Slot idx=%d message dropped after %0.1fs pending (mapping/channel_info never received): %r",
                        idx, now - ts, msg)
                else:
                    still_valid.append((ts, msg))
            if still_valid:
                self._pending_slot_messages[idx] = still_valid
            else:
                stale_slots.append(idx)
        for idx in stale_slots:
            del self._pending_slot_messages[idx]
