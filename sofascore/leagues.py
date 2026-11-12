"""The Sofascore tournaments we collect (BAS-227): AO-priced, betsapi-primary canonical leagues, plus
Pro B Germany and the Baltic women's league, which no other source serves any more, plus the leagues
where Sofascore has player box scores and RealGM, Flashscore and Basketball API have no stats.

Club competitions only: no national-team contests and no 3x3.

Each entry is a Sofascore unique tournament id, its sport and the canonical league it covers. The
canonical id is a pointer for the later mapping (BAS-350); the collector writes nothing outside the
``sofascore_*`` tables. Add a league here and the next backfill or daily run picks it up.
"""

from dataclasses import dataclass

from sofascore.sports import BASKETBALL, Sport


@dataclass(frozen=True)
class SofascoreLeague:
    """One Sofascore unique tournament to collect."""

    tid: int
    sport: Sport
    canonical_league_id: int
    name: str


LEAGUES: tuple[SofascoreLeague, ...] = (
    SofascoreLeague(14251, BASKETBALL, 1143, "Serie B"),
    SofascoreLeague(21307, BASKETBALL, 1144, "Liga Argentina"),
    SofascoreLeague(37703, BASKETBALL, 1147, "Liga Femenina Challenge, Women"),
    SofascoreLeague(23420, BASKETBALL, 1145, "CN1 Basquetebol"),
    SofascoreLeague(38527, BASKETBALL, 1076, "NBL, Division One Men"),
    SofascoreLeague(30074, BASKETBALL, 1148, "Campeonato Nacional da 1ª Divisao Feminina"),
    SofascoreLeague(17343, BASKETBALL, 1020, "El Salvador LMB"),
    SofascoreLeague(29056, BASKETBALL, 1024, "NLB"),
    SofascoreLeague(27568, BASKETBALL, 1033, "NBL"),
    SofascoreLeague(1934, BASKETBALL, 1142, "Belgian Basketball 2nd Division"),
    SofascoreLeague(23748, BASKETBALL, 1153, "Liga 1 Masculin"),
    SofascoreLeague(38233, BASKETBALL, 1021, "B.League Next"),
    SofascoreLeague(30073, BASKETBALL, 1154, "1st Division Men"),
    SofascoreLeague(29644, BASKETBALL, 1103, "2. MLS"),
    SofascoreLeague(26517, BASKETBALL, 1074, "NBL Women"),
    SofascoreLeague(29765, BASKETBALL, 1149, "1 Liga Kobiet"),
    SofascoreLeague(10267, BASKETBALL, 1032, "Big V, Women"),
    SofascoreLeague(23803, BASKETBALL, 1056, "Poland 2nd Basketball League"),
    SofascoreLeague(3084, BASKETBALL, 1029, "Big V"),
    SofascoreLeague(26785, BASKETBALL, 1055, "Liga Nacional Femenina Chile"),
    SofascoreLeague(28751, BASKETBALL, 1028, "BSNF, Women"),
    SofascoreLeague(24912, BASKETBALL, 1044, "Rwanda Basketball League"),
    SofascoreLeague(23935, BASKETBALL, 1139, "Saku I liiga"),
    SofascoreLeague(25878, BASKETBALL, 1088, "1. Division B"),
    SofascoreLeague(26277, BASKETBALL, 1031, "LFB, Women"),
    SofascoreLeague(25668, BASKETBALL, 1053, "NBL Men"),
    SofascoreLeague(30966, BASKETBALL, 1048, "Dislog Basketball League"),
    SofascoreLeague(27754, BASKETBALL, 1066, "Divisional Tercera de Ascenso"),
    SofascoreLeague(23930, BASKETBALL, 1030, "Iraqi Professional League"),
    SofascoreLeague(37520, BASKETBALL, 1135, "UAAP, Basketball"),
    SofascoreLeague(27527, BASKETBALL, 1037, "Rwanda Basketball League Women"),
    SofascoreLeague(25751, BASKETBALL, 1051, "LSB"),
    SofascoreLeague(24922, BASKETBALL, 1185, "A league"),
    SofascoreLeague(25532, BASKETBALL, 1128, "Liga Ouro"),
    SofascoreLeague(14574, BASKETBALL, 1022, "Tunisia National A League"),
    SofascoreLeague(24275, BASKETBALL, 1096, "WABA 2 League"),
    SofascoreLeague(28885, BASKETBALL, 1091, "Catarinense"),
    SofascoreLeague(29918, BASKETBALL, 1162, "1st Division Women"),
    SofascoreLeague(26235, BASKETBALL, 1082, "Bahrain Cup"),
    SofascoreLeague(28866, BASKETBALL, 1084, "IBL All Indonesian"),
    SofascoreLeague(37825, BASKETBALL, 1042, "NCAA, Basketball"),
    SofascoreLeague(29240, BASKETBALL, 1113, "Arab Women's Clubs Championship"),
    SofascoreLeague(13647, BASKETBALL, 1601, "LDA"),
    SofascoreLeague(38137, BASKETBALL, 1118, "Women's Cup"),
    SofascoreLeague(25533, BASKETBALL, 1542, "Golden Square"),
    SofascoreLeague(24378, BASKETBALL, 1087, "FIBA Africa Women's Basketball League"),
    SofascoreLeague(31366, BASKETBALL, 1159, "Women's Cup"),
    SofascoreLeague(25381, BASKETBALL, 1158, "Coppa Italia A2 Women"),
    SofascoreLeague(28898, BASKETBALL, 1270, "Catarinense, Women"),
    SofascoreLeague(9466, BASKETBALL, 1140, "Supercoppa LNP, Serie A2"),
    SofascoreLeague(37127, BASKETBALL, 1714, "Euroleague SuperCup"),
    SofascoreLeague(24273, BASKETBALL, 1262, "Bulgarian Women's Basketball Championship"),
    SofascoreLeague(23647, BASKETBALL, 1073, "Paulista, 1ª Divisão"),
    SofascoreLeague(30548, BASKETBALL, 1513, "2. ZLS, Women"),
    SofascoreLeague(30720, BASKETBALL, 1264, "Super Cup"),
    SofascoreLeague(28273, BASKETBALL, 488, "Pro B"),
    SofascoreLeague(29729, BASKETBALL, 445, "WBBL, Women"),
    # Leagues where Sofascore has player box scores (mostly from 2023/24) and our other stats
    # sources have none or stopped. NCAA is left out: ~12,000 games a season would dominate the queue.
    SofascoreLeague(1580, BASKETBALL, 606, "NBA G League"),
    SofascoreLeague(23937, BASKETBALL, 441, "Philippines MPBL"),
    SofascoreLeague(11499, BASKETBALL, 475, "Israeli National League Basketball"),
    SofascoreLeague(29552, BASKETBALL, 500, "Super League"),
    SofascoreLeague(24105, BASKETBALL, 1425, "Latvia NBL"),
    SofascoreLeague(20078, BASKETBALL, 486, "Israeli Women Basketball Premier League"),
    SofascoreLeague(251, BASKETBALL, 793, "1. DBBL"),
    SofascoreLeague(29888, BASKETBALL, 794, "Liga Femenina"),
    SofascoreLeague(1538, BASKETBALL, 831, "Liga Femenina"),
    SofascoreLeague(21128, BASKETBALL, 588, "Finland Division 1A Women"),
    SofascoreLeague(11511, BASKETBALL, 665, "Iranian Basketball Super League"),
    SofascoreLeague(24412, BASKETBALL, 830, "Zain Basketball League"),
    SofascoreLeague(29868, BASKETBALL, 499, "National League"),
    SofascoreLeague(14089, BASKETBALL, 846, "Dominican Republic LNB"),
    SofascoreLeague(13906, BASKETBALL, 653, "Libobasquet"),
    SofascoreLeague(27960, BASKETBALL, 584, "National Basketball League"),
    SofascoreLeague(23944, BASKETBALL, 519, "Chinese Taipei TPBL"),
    SofascoreLeague(18639, BASKETBALL, 838, "Chinese Taipei P League"),
    SofascoreLeague(25873, BASKETBALL, 797, "LNB 2"),
    SofascoreLeague(29539, BASKETBALL, 651, "BLNO, Women"),
    SofascoreLeague(3074, BASKETBALL, 759, "Slovenia Cup"),
    SofascoreLeague(29358, BASKETBALL, 920, "Copa Espana"),
    SofascoreLeague(9543, BASKETBALL, 834, "Greek Women's Basketball League"),
    SofascoreLeague(24261, BASKETBALL, 1250, "Azerbaijan Basketball League"),
    SofascoreLeague(25147, BASKETBALL, 1069, "The League"),
    SofascoreLeague(31336, BASKETBALL, 941, "A1"),
    SofascoreLeague(26243, BASKETBALL, 1019, "Premier League"),
    SofascoreLeague(22877, BASKETBALL, 1343, "Liga Nacional Masculina"),
    SofascoreLeague(30090, BASKETBALL, 1090, "Women's Basketball Division A"),
    SofascoreLeague(23792, BASKETBALL, 1178, "Bulgarian BBL Division A"),
    SofascoreLeague(27041, BASKETBALL, 1591, "DIV I Women"),
    SofascoreLeague(30549, BASKETBALL, 1439, "I A ZCKL, Women"),
    # NBL1 Australia: one Sofascore tournament per conference, plus the national finals.
    SofascoreLeague(16739, BASKETBALL, 526, "NBL1, Central"),
    SofascoreLeague(16740, BASKETBALL, 526, "NBL1, West"),
    SofascoreLeague(16818, BASKETBALL, 526, "NBL1, North"),
    SofascoreLeague(16776, BASKETBALL, 526, "NBL1, South"),
    SofascoreLeague(18599, BASKETBALL, 526, "NBL1, East"),
    SofascoreLeague(18403, BASKETBALL, 526, "NBL1, East"),
    SofascoreLeague(18601, BASKETBALL, 526, "NBL1, Wildcard"),
    SofascoreLeague(13487, BASKETBALL, 526, "NBL1"),
    SofascoreLeague(16741, BASKETBALL, 512, "NBL1 Women, Central"),
    SofascoreLeague(16742, BASKETBALL, 512, "NBL1 Women, West"),
    SofascoreLeague(16819, BASKETBALL, 512, "NBL1 Women, North"),
    SofascoreLeague(16821, BASKETBALL, 512, "NBL1 Women, South"),
    SofascoreLeague(18600, BASKETBALL, 512, "NBL1 Women, East"),
    SofascoreLeague(18404, BASKETBALL, 512, "NBL1 Women, East"),
    SofascoreLeague(18552, BASKETBALL, 512, "NBL 1, Wildcard, Women"),
    SofascoreLeague(13488, BASKETBALL, 512, "NBL1, Women"),
)

BY_TID: dict[int, SofascoreLeague] = {league.tid: league for league in LEAGUES}
