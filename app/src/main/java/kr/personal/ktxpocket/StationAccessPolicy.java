package kr.personal.ktxpocket;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class StationAccessPolicy {
    final LinkedHashMap<String, Integer> stations = new LinkedHashMap<>();
    final String baseline;
    final int extraMinutes, boardingMinutes;

    StationAccessPolicy(Map<String, Integer> values, String baseline, int extraMinutes, int boardingMinutes) {
        if (values.isEmpty() || values.size() > 3) throw new IllegalArgumentException("역별 이동 시간은 1~3개로 등록하세요.");
        for (Map.Entry<String, Integer> entry : values.entrySet()) {
            String name = entry.getKey() == null ? "" : entry.getKey().trim();
            if (name.isEmpty() || stations.containsKey(name)) throw new IllegalArgumentException("같은 역은 한 번만 등록하세요.");
            if (entry.getValue() == null || entry.getValue() < 0 || entry.getValue() > 360)
                throw new IllegalArgumentException("역까지 걸리는 시간은 0~360분으로 입력하세요.");
            stations.put(name, entry.getValue());
        }
        this.baseline = baseline == null ? "" : baseline.trim();
        if (!stations.containsKey(this.baseline)) throw new IllegalArgumentException("현재 출발역의 이동 시간을 먼저 등록하세요.");
        if (extraMinutes < 0 || extraMinutes > 360 || boardingMinutes < 0 || boardingMinutes > 120)
            throw new IllegalArgumentException("추가 이동은 0~360분, 승차 여유는 0~120분으로 입력하세요.");
        this.extraMinutes = extraMinutes; this.boardingMinutes = boardingMinutes;
    }

    int minutesFor(String name) {
        Integer result = stations.get(name.trim());
        if (result == null) throw new IllegalArgumentException(name + "역까지 걸리는 시간을 등록하세요.");
        return result;
    }

    boolean eligible(String name) { return minutesFor(name) <= minutesFor(baseline) + extraMinutes; }

    List<String> eligibleStations() {
        List<String> names = new ArrayList<>();
        for (String name : stations.keySet()) if (eligible(name)) names.add(name);
        names.sort(Comparator.comparingInt(this::minutesFor));
        names.remove(baseline); names.add(0, baseline);
        return names;
    }
}
