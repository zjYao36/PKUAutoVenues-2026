"""按完整时间段和指定场地选择预约，供配置入口及原命令行共用。"""


def select_trades(info, target_date, target_times, preferred_spaces, *, strict_spaces=False, end_times=None):
    slots = sorted(info.get("spaceTimeInfo", []), key=lambda item: item["beginTime"])
    rows = info.get("reservationDateSpaceInfo", {}).get(target_date, [])
    if not rows:
        return None

    for candidate, (begin_time, count) in enumerate(target_times):
        indexes = [index for index, slot in enumerate(slots) if slot["beginTime"] == begin_time]
        if not indexes:
            continue
        start = indexes[0]
        if end_times is not None:
            end_time = end_times[candidate]
            selected_slots = []
            cursor = begin_time
            for slot in slots[start:]:
                if slot["beginTime"] != cursor or slot["endTime"] > end_time:
                    break
                selected_slots.append(slot)
                cursor = slot["endTime"]
                if cursor == end_time:
                    break
            if not selected_slots or cursor != end_time:
                continue
        else:
            # 原命令行保留按时段数选择的用法。
            selected_slots = slots[start:start + count]
            if strict_spaces and len(selected_slots) != count:
                continue
        if not selected_slots:
            continue
        if any(left["endTime"] != right["beginTime"] for left, right in zip(selected_slots, selected_slots[1:])):
            continue

        available = {}
        for row in rows:
            name = row["spaceName"]
            if strict_spaces and name not in preferred_spaces:
                continue
            trades = [row.get(str(slot["id"]), {}) for slot in selected_slots]
            if not all(trade.get("reservationStatus") == 1 and trade.get("orderFee") is not None for trade in trades):
                continue
            available[name] = [
                {
                    "timeId": str(slot["id"]), "beginTime": slot["beginTime"], "endTime": slot["endTime"],
                    "spaceId": str(row["id"]), "spaceName": name, "orderFee": float(trade["orderFee"]),
                }
                for slot, trade in zip(selected_slots, trades)
            ]
        for name in preferred_spaces:
            if name in available:
                return available[name]
        if available and not strict_spaces:
            return next(iter(available.values()))
    return None
