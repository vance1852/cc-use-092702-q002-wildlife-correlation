from __future__ import annotations

import unittest
from decimal import Decimal

from sighting_linkage.linkage import StoredRecord, build_pairwise, compare_records, connected_components


def record(
    record_id: str,
    *,
    observed_at: str = "2026-09-27T22:00:00+00:00",
    time_uncertainty_seconds: int = 60,
    valley: str = "东沟",
    latitude: str | None = None,
    longitude: str | None = None,
    radius: int | None = None,
    species_code: str = "MOS_BERE",
    species_status: str = "probable",
    species_confidence: str = "0.8",
    image_class: str | None = None,
    image_confidence: str | None = None,
    image_tags: tuple[str, ...] = (),
    observer_id: str = "o1",
    observer_role: str = "patrol",
    sensitive: bool = False,
) -> StoredRecord:
    return StoredRecord(
        record_id=record_id,
        observer_id=observer_id,
        observer_role=observer_role,
        observed_at=observed_at,
        time_uncertainty_seconds=time_uncertainty_seconds,
        valley=valley,
        latitude=None if latitude is None else Decimal(latitude),
        longitude=None if longitude is None else Decimal(longitude),
        location_radius_meters=radius,
        location_precision_text="测试精度描述",
        species_code=species_code,
        species_status=species_status,
        species_confidence=Decimal(species_confidence),
        image_class=image_class,
        image_confidence=None if image_confidence is None else Decimal(image_confidence),
        image_tags=image_tags,
        sensitive=sensitive,
    )


class LinkageScoringTests(unittest.TestCase):
    def test_three_source_donggou_scenario_is_one_component(self) -> None:
        community = record(
            "community",
            observed_at="2026-09-27T21:55:00+00:00",
            time_uncertainty_seconds=1800,
            species_status="probable",
            species_confidence="0.7",
            observer_id="c1",
            observer_role="community_ranger",
        )
        research = record(
            "research",
            observed_at="2026-09-27T22:02:00+00:00",
            time_uncertainty_seconds=120,
            latitude="33.6512",
            longitude="108.5534",
            radius=60,
            species_confidence="0.6",
            image_class="musk_deer_genus",
            image_confidence="0.45",
            image_tags=("伏地不动",),
            observer_id="r1",
            observer_role="researcher",
            sensitive=True,
        )
        patrol = record(
            "patrol",
            observed_at="2026-09-27T22:05:00+00:00",
            time_uncertainty_seconds=60,
            species_status="confirmed",
            species_confidence="0.9",
            observer_id="p1",
            observer_role="patrol",
            sensitive=True,
        )
        pairs = build_pairwise([community, research, patrol])
        self.assertEqual(len(pairs), 3)
        self.assertTrue(all(pair["candidate"] for pair in pairs))
        for pair in pairs:
            self.assertIn("dimensions", pair)
            self.assertIn("time", pair["dimensions"])
            self.assertTrue(pair["supporting_evidence"])
        groups = connected_components([community, research, patrol], pairs)
        self.assertEqual(groups, [["community", "patrol", "research"]])

    def test_deterministic_scores_across_input_order(self) -> None:
        a = record("a", observer_id="o1", observer_role="patrol")
        b = record(
            "b",
            observed_at="2026-09-27T22:03:00+00:00",
            observer_id="o2",
            observer_role="researcher",
            species_confidence="0.9",
        )
        first = compare_records(a, b)
        second = compare_records(b, a)
        self.assertEqual(first["score"], second["score"])
        self.assertEqual(first["dimensions"], second["dimensions"])
        self.assertEqual(build_pairwise([a, b]), build_pairwise([b, a]))

    def test_time_veto_far_apart(self) -> None:
        a = record("a", observed_at="2026-09-27T22:00:00+00:00", time_uncertainty_seconds=60)
        b = record("b", observed_at="2026-09-28T04:30:00+00:00", time_uncertainty_seconds=60)
        result = compare_records(a, b)
        self.assertFalse(result["candidate"])
        self.assertTrue(any("时间互斥" in reason for reason in result["exclusion_reasons"]))

    def test_species_veto_when_both_confident_but_different(self) -> None:
        a = record("a", species_code="MOS_BERE", species_status="confirmed", species_confidence="0.95")
        b = record("b", species_code="CAP_HICOLOR", species_status="confirmed", species_confidence="0.95")
        result = compare_records(a, b)
        self.assertFalse(result["candidate"])
        self.assertTrue(any("物种互斥" in reason for reason in result["exclusion_reasons"]))

    def test_species_mismatch_kept_when_one_side_suspected(self) -> None:
        a = record("a", species_code="MOS_BERE", species_status="suspected", species_confidence="0.4")
        b = record("b", species_code="CAP_HICOLOR", species_status="probable", species_confidence="0.6")
        result = compare_records(a, b)
        self.assertEqual(result["dimensions"]["species"]["score"], 0.3)
        self.assertFalse(any("物种互斥" in reason for reason in result["exclusion_reasons"]))

    def test_coordinate_circles_overlap_and_separation(self) -> None:
        # 两点约相距 60 米，各自半径 50 米 -> 圆相交，空间满分相容。
        a = record("a", latitude="33.6500", longitude="108.5500", radius=50)
        b = record("b", latitude="33.6505", longitude="108.5502", radius=50)
        overlap = compare_records(a, b)
        self.assertTrue(overlap["dimensions"]["space"]["detail"]["circles_overlap"])
        # 半径各 10 米仍然分离 -> 空间分下降并给出米数解释。
        c = record("c", latitude="33.6500", longitude="108.5500", radius=10)
        d = record("d", latitude="33.6505", longitude="108.5502", radius=10)
        separated = compare_records(c, d)
        self.assertFalse(separated["dimensions"]["space"]["detail"]["circles_overlap"])
        self.assertGreater(separated["dimensions"]["space"]["detail"]["separation_meters"], 0)

    def test_space_veto_beyond_hard_limit(self) -> None:
        a = record("a", latitude="33.65", longitude="108.55", radius=50)
        b = record("b", latitude="33.95", longitude="108.95", radius=50)
        result = compare_records(a, b)
        self.assertFalse(result["candidate"])
        self.assertTrue(any("位置互斥" in reason for reason in result["exclusion_reasons"]))

    def test_hidden_coordinates_same_valley_neutral(self) -> None:
        a = record("a")
        b = record("b", observed_at="2026-09-27T22:02:00+00:00")
        result = compare_records(a, b)
        self.assertEqual(result["dimensions"]["space"]["detail"]["basis"], "valley_only")
        self.assertEqual(result["dimensions"]["space"]["score"], 0.5)

    def test_image_class_hierarchy_compatible(self) -> None:
        a = record("a", image_class="forest_musk_deer", image_confidence="0.9")
        b = record("b", image_class="musk_deer_genus", image_confidence="0.8")
        result = compare_records(a, b)
        self.assertGreaterEqual(result["dimensions"]["image"]["score"], 0.55)

    def test_no_animal_image_is_weak_counterevidence_not_veto(self) -> None:
        a = record("a", image_class="forest_musk_deer", image_confidence="0.9")
        b = record("b", image_class="no_animal", image_confidence="0.8")
        result = compare_records(a, b)
        self.assertIn("image", result["dimensions"])
        self.assertLess(result["dimensions"]["image"]["score"], 0.3)

    def test_same_observer_is_not_independent_corroboration(self) -> None:
        a = record("a", observer_id="same", observer_role="patrol")
        b = record("b", observer_id="same", observer_role="patrol")
        result = compare_records(a, b)
        self.assertEqual(result["dimensions"]["observer"]["score"], 0.5)
        cross = compare_records(
            record("a", observer_id="x", observer_role="patrol"),
            record("b", observer_id="y", observer_role="researcher"),
        )
        self.assertEqual(cross["dimensions"]["observer"]["score"], 1.0)

    def test_missing_image_dimension_redistributes_weight(self) -> None:
        a = record("a", observer_id="o1", observer_role="patrol")
        b = record(
            "b",
            observed_at="2026-09-27T22:01:00+00:00",
            observer_id="o2",
            observer_role="researcher",
        )
        result = compare_records(a, b)
        self.assertNotIn("image", result["dimensions"])
        # 其余维度都很高时，没有影像不应拖低综合分。
        self.assertGreater(float(result["score"]), 0.7)


if __name__ == "__main__":
    unittest.main()
