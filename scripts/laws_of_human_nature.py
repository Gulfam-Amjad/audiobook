"""Build the Laws of Human Nature book summary for Mighty Chapters.

    python scripts/laws_of_human_nature.py fetch   # licensed stills and footage
    python scripts/laws_of_human_nature.py sheet   # contact sheet for review
    python scripts/laws_of_human_nature.py spec    # write the film spec for make_film.py

An audio-first summary of Robert Greene's book, all eighteen laws, in our own
words. Real footage under the cold open, then one still per shot with a slow
camera move, a chapter card for every law and key points as labels.
Nothing is uploaded.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ffmpeg, stock_media  # noqa: E402

FILM_DIR = Path(r"D:\AIVideoManager\films\laws-of-human-nature")
STILLS = Path(r"D:\youtube_library\laws-of-human-nature-stills")
SPEC = FILM_DIR / "spec.json"
PICKS = FILM_DIR / "picks.json"
SHEET = FILM_DIR / "contact-sheet"
SPEAKING_WPM = 150
#: What the director keeps of a chapter title and a label.
MAX_CHAPTER = 60
MAX_LOWER = 80


@dataclass
class S:
    key: str
    query: str
    narration: str
    kind: str = "image"  # image or video
    beat: str = "build"
    mood: str = "steady"
    chapter: str | None = None
    lower: str | None = None
    transition: str = "cut"
    ambience: str = "room"
    pause: float | None = None
    fallback: str = "portrait painting"
    #: Search Wikimedia Commons only: archive portraits are upright and lose
    #: the ranking to wide stock photos otherwise.
    commons: bool = False


SCENES: list[S] = [
    # Cold open: real footage, no lecture first.
    S("s01", "crowd walking",
      "Everyone you have ever met is wearing a mask. Your boss. Your closest friend. The "
      "stranger who smiles at you on the train. And right now, without even knowing it, you "
      "are wearing one too.",
      kind="video", beat="hook", mood="tense", ambience="city", fallback="people city"),
    S("s02", "woman eye",
      "Behind those masks, ancient forces are pulling the strings. Envy that sounds like "
      "praise. Fear that dresses itself up as logic. Anger hiding behind a polite smile. Most "
      "people never see these forces. They only feel the damage, and wonder why the same "
      "painful patterns keep coming back into their lives.",
      kind="video", mood="tense", fallback="face portrait"),
    S("s03", "chess",
      "Robert Greene spent years tracing those forces through history. Through the assembly "
      "of ancient Athens, the courts of kings and queens, the boardrooms of Hollywood, and a "
      "quiet farmhouse in the American South. Saints and tyrants. Geniuses and frauds. Out of "
      "their lives, he drew eighteen laws that govern how all of us think, feel, and act.",
      kind="video", mood="driving", fallback="chess board"),
    S("s04", "venetian mask",
      "This is a summary of The Laws of Human Nature, told in two parts. Chapter by chapter, "
      "we will go through all eighteen laws, nine in each part. The story behind each one. The hidden machinery it "
      "reveals. And what you can actually do with it. Fair warning. Once you start to see "
      "these laws at work, you will never look at people the same way again.",
      kind="video", beat="reveal", mood="epic", pause=1.2, fallback="venetian mask"),

    # Introduction
    S("s05", "old books library",
      "A quick note before we begin. This is an independent summary and commentary, told "
      "entirely in our own words. It is no substitute for the book itself, which runs to "
      "more than six hundred pages, packed with stories and detail we simply cannot fit "
      "here. If these ideas grab you, read the whole thing. Now, Greene's starting point is "
      "simple, and a little uncomfortable.",
      mood="reflective", chapter="Before we begin|The Student of Human Nature",
      lower="Robert Greene, Author of The Laws of Human Nature", fallback="library books"),
    S("s06", "ancient greek statue",
      "We like to believe we are rational creatures, in charge of our own choices. We are "
      "not. We are social animals, shaped over hundreds of thousands of years to survive in "
      "groups, and that wiring still runs underneath everything we do. Greene asks us to "
      "become students of human nature. Calm observers who see people as they really are, "
      "starting with the person in the mirror. So let's begin with the law that sits beneath "
      "all the others.",
      mood="reflective", fallback="marble statue"),

    # Law 1
    S("s07", "Ancient Greece Marble Bust of Pericles",
      "Law one. The Law of Irrationality. Picture Athens, roughly two and a half thousand "
      "years ago, at the height of its golden age. For nearly three decades the city was "
      "guided by one man, Pericles. What made him unusual was not courage, or charm. It was "
      "restraint. When the Assembly boiled with anger, Pericles stepped back, waited for the "
      "heat to pass, and only then decided. The Greeks would have said he listened to "
      "Athena, the goddess of practical wisdom.",
      mood="steady", chapter="Law 1 · Master your emotional self|Irrationality",
      lower="Pericles, Leader of Athens", fallback="Pericles", commons=True),
    S("s08", "Vinzenz Katzler Pericles at the body of his son",
      "When war with Sparta broke out, Pericles chose patience. Avoid the big battle, protect "
      "the city, let the enemy wear itself out. Then a plague swept through Athens, and "
      "Pericles himself died. Without him, the Athenians gave in to fear, pride, and the "
      "thrill of revenge. Years later they voted to send a vast fleet to conquer Sicily. "
      "When a cautious general warned them it was madness, they doubled the expedition. It "
      "ended in total disaster, and Athens never recovered.",
      mood="dark", pause=1.0, fallback="Acropolis", commons=True),
    S("s09", "brain",
      "Greene's point is that we are all Athenians. Our emotions decide first, and our reason "
      "arrives later to write the excuse. He names six biases that feel like clear thinking "
      "from the inside. We look for evidence that confirms what we already believe. We trust "
      "a belief more because we hold it strongly. We judge people by their surface. We "
      "absorb the opinions of our group. We blame others for our mistakes. And we quietly "
      "assume we are smarter and more decent than everyone else.",
      mood="steady",
      lower="Six biases, Confirmation · Conviction · Appearance · Group · Blame · Superiority",
      fallback="thinking"),
    S("s10", "calm lake",
      "So how do we fight back? First, know what inflames you. Sudden wins and losses, rising "
      "pressure, provoking personalities, and the heat of a crowd all push us toward our "
      "worst decisions. Second, when an emotion rises, dig for its root. Where does this "
      "feeling really come from? Third, stretch your reaction time. Wait a day before "
      "sending the angry message. And finally, accept people as facts, like the weather. You "
      "stop wasting energy wishing they were different, and start seeing them clearly.",
      mood="hopeful",
      lower="The rational self, Pause · Dig to the root · Accept people as facts",
      fallback="lake"),

    # Law 2
    S("s11", "Joseph Stalin",
      "Law two. The Law of Narcissism. Every one of us is in love with ourselves to some "
      "degree. That is normal. The question is how deep it runs. Greene describes a "
      "spectrum. At the far end sits the deep narcissist, someone with no stable sense of "
      "self, who needs a constant supply of attention just to hold together. Joseph Stalin "
      "is his darkest example. Charming one moment, terrifying the next, keeping everyone "
      "around him off balance, and dependent on his mood.",
      mood="dark", chapter="Law 2 · Transform self-love into empathy|Narcissism",
      lower="Joseph Stalin, The controlling narcissist", fallback="Stalin"),
    S("s12", "Endurance Final Sinking",
      "At the healthy end stands a very different man. In nineteen fifteen, the explorer "
      "Ernest Shackleton watched the Antarctic pack crush his ship, the Endurance, leaving "
      "his crew stranded on frozen sea. For months he held them together. He studied every "
      "man's moods. He noticed who was close to breaking, and kept them near him. He gave "
      "away his own mittens. Against every expectation, not one man of that crew died. His "
      "self-love had been turned outward, into attention.",
      mood="hopeful", ambience="wind", lower="Ernest Shackleton, Antarctica, 1915",
      fallback="Ernest Shackleton", commons=True),
    S("s13", "two people talking",
      "How do you spot the deep narcissist? They are extremely sensitive to criticism. They "
      "need to control the people around them. They treat others as extensions of "
      "themselves, useful only while they reflect something back. Every conversation drifts "
      "back to them. And beware the narcissistic leader most of all, because that "
      "confidence and charisma can be intoxicating. People follow for a while, until they "
      "discover the relationship only ever runs one way.",
      mood="tense",
      lower="Signs of deep narcissism, Touchy · Controlling · Treats people as tools",
      fallback="conversation"),
    S("s14", "hands holding",
      "The cure is empathy, and Greene treats it as a skill you can train. Start with an "
      "attitude of curiosity. Assume you don't really know what is going on inside another "
      "person. Then tune in to their mood, through tone of voice and body language. Next, "
      "think analytically. What shaped them? What were their early years like? And finally, "
      "practise constantly, with as many different kinds of people as you can. Every "
      "conversation becomes a lesson in another human mind.",
      mood="hopeful", lower="Four parts of empathy, Attitude · Feeling · Analysis · Practice",
      fallback="people talking"),

    # Law 3
    S("s15", "Milton Erickson",
      "Law three. The Law of Role-playing. At seventeen, a young American named Milton "
      "Erickson was struck down by polio. Paralysed, at first barely able to speak, he lay in "
      "bed day after day with nothing to do but watch his family. And slowly, he noticed "
      "something. His sisters would say yes while their faces said no. Tone of voice "
      "betrayed what words concealed. Erickson had stumbled into a hidden language, and he "
      "would build a famous career in psychiatry on reading it.",
      mood="steady", chapter="Law 3 · See through people's masks|Role-playing",
      lower="Milton Erickson, Psychiatrist", fallback="vintage hospital bed"),
    S("s16", "theatre masks",
      "We all learn early to wear social masks. We smile when we are bored, nod when we "
      "disagree, and play the role each situation expects. But the masks leak. Faces, "
      "voices, posture, and nervous hands give away what we really feel. Greene focuses on "
      "three kinds of signals. Is this person warming to me, or cooling? Do they feel "
      "dominant, or submissive? And are they trying to deceive me? The key, every time, is "
      "a mismatch between what is said and what is shown.",
      mood="steady", lower="Three signals, Like or dislike · Dominance · Deception",
      fallback="mask"),
    S("s17", "crossed arms",
      "A few clues worth knowing. Real dislike shows in tight lips, a stiff neck, arms that "
      "fold as you make your point, or feet that turn away while the face keeps smiling. "
      "Comfort looks like leaning in, loose shoulders, and quietly mirroring your posture. "
      "Confidence looks relaxed and unhurried. And watch for the overly animated "
      "performance. When someone becomes extra warm, extra certain, or extra righteous, "
      "they are often working hard to hide the opposite.",
      mood="tense", fallback="body language"),
    S("s18", "theatre stage",
      "Here is the twist. Greene does not tell us to rip off our masks. Life is theatre, and "
      "refusing to play only makes you look awkward. Instead, play your role consciously. "
      "Manage your own signals. Show genuine warmth and calm confidence. Adapt to your "
      "audience. Take extra care with first impressions, because people decide quickly. The "
      "goal is to become a skilled actor who can also see straight through everyone else's "
      "performance.",
      mood="hopeful", lower="Impression management, Play your role, consciously",
      fallback="theater"),

    # Law 4
    S("s19", "Howard Hughes",
      "Law four. The Law of Compulsive Behavior. To the public, Howard Hughes was the "
      "ultimate American maverick. Billionaire, record-breaking pilot, Hollywood producer, a "
      "rugged genius who did everything his own way. But behind the myth was a pattern. "
      "Raised by an anxious, smothering mother and a distant father, Hughes grew up with a "
      "desperate need to control everything around him. When both parents died young and he "
      "inherited the family business, that need was suddenly let loose on the world.",
      mood="steady", chapter="Law 4 · See people's true character|Compulsive Behavior",
      lower="Howard Hughes, Tycoon, pilot, film producer", fallback="Hughes"),
    S("s20", "Hughes H-4 Hercules",
      "Again and again, Hughes hired brilliant people, promised them real authority, and "
      "then could not stop interfering. Films ran years over schedule. Aircraft projects "
      "stalled under his endless changes. His giant wooden flying boat, nicknamed the Spruce "
      "Goose, flew exactly once, for about a mile, and never again. The reputation said "
      "genius. The record said the same mistake, repeated for decades. Greene's lesson is "
      "blunt. People rarely do something important just once. Their patterns return.",
      mood="dark", pause=1.0, lower="The Spruce Goose, One flight, 1947",
      fallback="Spruce Goose"),
    S("s21", "gold nugget",
      "So ignore the image people project, and study their character instead. Character is "
      "built in layers. Some comes from our genes, some from our earliest attachments, and "
      "the rest from habits laid down over a lifetime. To read it, look at how someone has "
      "acted over many years, not what they say about themselves. Watch them under stress, "
      "and watch them when they gain power. Notice small details, like how they treat a "
      "waiter. Strong character is rare, and worth more than gold.",
      mood="steady",
      lower="Read character, The pattern · Under stress · With power · Small details",
      fallback="gold"),
    S("s22", "storm clouds",
      "Greene also sketches toxic types to steer clear of. The hyperperfectionist who must "
      "control every detail, and burns out the whole team. The eternal rebel, stuck in "
      "adolescence. The drama magnet, exciting at first and exhausting later. The big "
      "talker, full of grand plans that never get built. And the saviour, who rescues you "
      "only to keep you dependent. As for your own patterns, the only escape is to see them "
      "honestly. Awareness won't erase them, but it loosens their grip.",
      mood="tense",
      lower="Toxic types, Perfectionist · Rebel · Drama magnet · Big talker · Saviour",
      fallback="dark clouds"),

    # Law 5
    S("s23", "Coco Chanel",
      "Law five. The Law of Covetousness. Gabrielle Chanel grew up with almost nothing. "
      "After her mother died, she was raised in a convent orphanage, surrounded by plain "
      "black and white. Years later, as Coco Chanel, she would turn that austere world into "
      "the most coveted style in Paris. She understood something most of us miss. We do "
      "not chase what we have. We chase what we lack, and above all, what feels just out of "
      "reach.",
      mood="steady", chapter="Law 5 · Become an elusive object of desire|Covetousness",
      lower="Coco Chanel, Fashion designer", fallback="Chanel"),
    S("s24", "perfume bottle",
      "Chanel wrapped herself and her work in mystery. She invented stories about her past, "
      "kept her methods secret, and surrounded her perfume with an air of exclusivity that "
      "had women hunting for it. She borrowed from men's clothing at a time when that felt "
      "daring, even forbidden. Every move opened a gap between what people could have and "
      "what they could imagine. And desire rushed in to fill that gap.",
      mood="steady", fallback="perfume"),
    S("s25", "woman silhouette window",
      "Greene draws out the mechanics. Too much presence smothers interest, while a little "
      "absence sharpens it. So don't reveal everything at once. Leave some mystery about who "
      "you are and what you will do next. People also want what others want, so visible "
      "demand creates more demand. And anything with a hint of the forbidden pulls hard on "
      "our imagination. Desire lives in the imagination, not in the object itself.",
      mood="tense",
      lower="Stir desire, Strategic absence · Rivalry · A hint of the forbidden",
      fallback="silhouette"),
    S("s26", "fence meadow",
      "But this law also points straight back at us. Our own desire keeps drifting toward "
      "the greener grass. The other job, the other partner, the other life. Greene's answer "
      "is not to stop wanting, but to aim our desire at reality itself. To become hungry "
      "for a deeper connection with the life we actually have. That brings calm, focus, and "
      "the power to change what can truly be changed.",
      mood="reflective", lower="The supreme desire, Want what is real",
      fallback="green field"),

    # Law 6
    S("s27", "South Sea Bubble",
      "Law six. The Law of Shortsightedness. In seventeen twenty, all of England caught a "
      "fever. The South Sea Company, promising vast profits from trade with South America, "
      "watched its shares soar. Lords, servants, merchants, and clergymen rushed to buy. "
      "Every week the price climbed higher, and every person who held back looked like a "
      "fool. Even Isaac Newton, perhaps the greatest mind of his age, could not resist.",
      mood="tense", chapter="Law 6 · Elevate your perspective|Shortsightedness",
      lower="The South Sea Bubble, London, 1720", fallback="South Sea Company"),
    S("s28", "Sir Isaac Newton by Sir Godfrey Kneller",
      "Newton actually bought early, sold, and made a tidy profit. Then he watched his "
      "friends grow richer, and jumped back in near the top. When the bubble burst, the "
      "shares collapsed, fortunes vanished, and ruin spread across the country. Newton lost "
      "a fortune. He is said to have remarked that he could calculate the motion of the "
      "heavens, but not the madness of people. The man who understood gravity had forgotten "
      "that prices fall too.",
      mood="dark", pause=1.0, lower="Isaac Newton, Lost a fortune in the crash",
      fallback="Newton", commons=True),
    S("s29", "stock market",
      "The problem is our wiring. We react most strongly to what is vivid and immediate. "
      "Greene names four warning signs. Unintended consequences, when we act without "
      "thinking two or three steps ahead. Tactical hell, when we are trapped in endless "
      "small battles and forget what we were fighting for. Ticker-tape fever, when we react "
      "to every new headline. And getting lost in trivia, drowning in detail, like the "
      "Spanish king who planned his Armada down to the last barrel and forgot about the "
      "weather.",
      mood="tense",
      lower="Four traps, Side effects · Tactical hell · Ticker-tape fever · Trivia",
      fallback="stock chart"),
    S("s30", "mountain summit view",
      "The antidote is to raise your perspective. When something exciting or alarming "
      "happens, deliberately step back. Imagine how this decision will look in six months, "
      "and in five years. Look for the larger trend beneath the noise. Keep your long-term "
      "goals clear, so you can weigh each short-term temptation against them. And keep your "
      "distance from impulsive people, because urgency is contagious. Treat time as your "
      "ally, not your enemy. Patience, in Greene's view, is a kind of superpower.",
      mood="hopeful", lower="The farsighted mind, Step back · Think in years · Then act",
      fallback="mountain"),

    # Law 7
    S("s31", "Lyndon B. Johnson",
      "Law seven. The Law of Defensiveness. When Lyndon Johnson arrived in the United States "
      "Senate in nineteen forty-nine, he was a junior nobody in a club run by older men. The "
      "most powerful of them was Richard Russell of Georgia, a lifelong bachelor who lived "
      "for the Senate. Johnson did not try to impress him. Instead, he became his eager "
      "student. He asked for Russell's advice, shared his interests, and kept him company on "
      "lonely weekends.",
      mood="steady", chapter="Law 7 · Confirm people's self-opinion|Defensiveness",
      lower="Lyndon B. Johnson, United States Senate, 1949", fallback="Lyndon Johnson"),
    S("s32", "Richard Russell in 1952",
      "Russell came to see Johnson almost as the son he never had, and opened every door in "
      "the Senate for him. With the liberal Hubert Humphrey, Johnson played a different "
      "note, praising his idealism while gently steering him toward deals that could "
      "actually pass. Within four years, Johnson was leading his party in the Senate, the "
      "youngest man ever to hold that post. He rarely won by arguing. He let people reach "
      "his conclusions themselves, and then defend them as their own.",
      mood="driving", lower="Richard Russell, Senator from Georgia",
      fallback="United States Capitol", commons=True),
    S("s33", "handshake",
      "Why does this work? Because everyone carries a self-opinion, a private story about who "
      "they are. Almost always, it includes three beliefs. I am independent. I am "
      "intelligent. And I am basically a good person. Challenge any of those, even "
      "indirectly, and the walls go up. Arguments bounce off. But confirm them, and people "
      "relax. Feeling secure, they open up. The fastest way to change a mind is to make its "
      "owner feel respected.",
      mood="steady", lower="The self-opinion, I am free · I am smart · I am good",
      fallback="shaking hands"),
    S("s34", "coffee conversation",
      "So become a deep listener, and let others be the stars of the conversation. Set the "
      "mood with your own warmth and calm, because emotions are contagious. Ask people for "
      "advice, and remind them of the generous things they have done before. Praise effort, "
      "not just talent. With stubborn people, use their resistance instead of fighting it. "
      "And turn the law on yourself. As we age, our minds harden. Treat your opinions like "
      "building blocks you are willing to rearrange.",
      mood="hopeful",
      lower="The persuader, Listen · Set the mood · Validate · Reassure",
      fallback="cafe"),

    # Law 8
    S("s35", "Anton Tschechow Lewitan",
      "Law eight. The Law of Self-sabotage. Anton Chekhov grew up in a small Russian port "
      "town with a father who beat his children almost every day. When the family business "
      "collapsed, his parents fled to Moscow to escape their debts, leaving the teenage "
      "Anton behind. Alone and poor, he could easily have grown bitter. Instead, he made a "
      "remarkable choice. He decided he would try to understand his father, rather than "
      "simply hate him.",
      mood="reflective", chapter="Law 8 · Change your attitude|Self-sabotage",
      lower="Anton Chekhov, Writer and doctor", fallback="Chekhov", commons=True),
    S("s36", "Tolstoy and chekhov",
      "He came to see that his father had been born a serf, beaten in his own childhood, "
      "worn down by a hard life. That did not excuse the cruelty, but it explained it. "
      "Chekhov forgave him, then took responsibility for the whole family, supporting them "
      "as a doctor while writing stories at night. Later he crossed Siberia to the brutal "
      "prison island of Sakhalin, to record the prisoners' lives. His generous view of "
      "people became the heart of his genius.",
      mood="hopeful", lower="The turn, From resentment to understanding",
      fallback="Russian village", commons=True),
    S("s37", "rain window",
      "Greene's insight is that your attitude, your habitual way of seeing the world, does "
      "not just colour what happens to you. It helps cause it. A hostile person sees "
      "enemies everywhere, provokes them, and calls the result proof. An anxious person "
      "avoids every risk and shrinks their own life. Greene describes five constricted "
      "attitudes: hostile, anxious, avoidant, depressive, and resentful. Each one creates "
      "the very world it expects.",
      mood="dark",
      lower="Constricted attitudes, Hostile · Anxious · Avoidant · Depressive · Resentful",
      fallback="rain"),
    S("s38", "sunrise",
      "The alternative is an expansive attitude. See yourself as an explorer, always "
      "searching for new ideas and experiences. Treat every setback as information and "
      "training, something that makes you tougher. Believe you are capable of more than you "
      "are doing now, because that belief changes what you attempt. And stop taking what "
      "people do so personally. Most of it has very little to do with you. Change the lens, "
      "and slowly, the circumstances change too.",
      mood="hopeful", lower="The expansive attitude, Explorer · Learner · Open to people",
      fallback="dawn"),

    # Law 9
    S("s39", "Richard M Nixon NARA 1972",
      "Law nine. The Law of Repression. Richard Nixon spent his career projecting control. "
      "Hard-working, disciplined, tough. The self-made man who would never crack. But "
      "beneath that image lay deep insecurity and resentment, a lifelong feeling of being "
      "looked down on by the elite. He never faced those feelings. He buried them. And what "
      "we bury does not disappear. It grows in the dark.",
      mood="dark", chapter="Law 9 · Confront your dark side|Repression",
      lower="Richard Nixon, 37th President of the United States", fallback="Nixon", commons=True),
    S("s40", "Watergate hotel",
      "Even as president, with enormous power in his hands, Nixon kept lists of enemies, "
      "secretly taped conversations, and approved dirty tricks against opponents. In "
      "nineteen seventy-two, men tied to his re-election campaign were caught breaking into "
      "the Democratic headquarters at the Watergate building. The cover-up that followed "
      "destroyed him. In nineteen seventy-four, he became the first American president to "
      "resign. His own shadow had brought him down.",
      mood="dark", pause=1.0, beat="reveal", lower="The Watergate scandal, 1972 to 1974",
      fallback="Nixon resignation", commons=True),
    S("s41", "silhouette man dark",
      "Borrowing from the psychologist Carl Jung, Greene calls this hidden side the shadow. "
      "It is everything we have pushed down to fit in. Selfish wants, aggression, "
      "insecurity. It leaks out in sudden outbursts, in fierce denials, in suspicious "
      "accidents, and above all in projection, when we condemn in others exactly what we "
      "refuse to see in ourselves. Be wary of extreme traits. The loud tough guy often hides "
      "softness. The spotless saint often hides a hunger for power.",
      mood="tense",
      lower="Signs of the shadow, Outbursts · Denial · Over-idealising · Projection",
      fallback="shadow"),
    S("s42", "woman mirror reflection",
      "The answer is not to fight your shadow, but to integrate it. First, see it. Notice "
      "your own outbursts, and the things you condemn most fiercely in others. Then accept "
      "it as part of who you are. Explore it, because the shadow holds raw creative energy. "
      "And finally, let some of it out in healthy ways. Be a little more assertive. Care a "
      "little less what people think. A person who owns their dark side seems more real, "
      "and more magnetic, than any polished saint. That closes part one. In part two, we turn "
      "to the laws that hide inside friendship, ambition, and the story of a whole generation.",
      mood="hopeful", lower="Integrate the shadow, See it · Accept it · Explore it · Show it",
      fallback="mirror"),

    # Law 10
    S("s43", "Portrait of Mary Wollstonecraft Shelley Bodleian",
      "Welcome to part two. In part one we covered the first nine laws, from the irrational "
      "mind to the hidden shadow. Now we continue. Law ten. The Law of Envy. In the early "
      "eighteen twenties, Mary Shelley, the young "
      "author of Frankenstein, was living in Italy with her husband, the poet Percy Shelley. "
      "They grew close to another couple, Edward and Jane Williams. Jane was charming, "
      "sweet, and attentive, everything a friend should be. Mary trusted her completely. "
      "What Mary did not see was that Jane quietly resented her intellect, her talent, and "
      "her fame.",
      mood="steady", chapter="Law 10 · Beware the fragile ego|Envy",
      lower="Mary Shelley, Author of Frankenstein", fallback="Shelley", commons=True),
    S("s44", "stormy sea waves",
      "In the summer of eighteen twenty-two, Percy and Edward drowned together when their "
      "boat went down in a storm. The two widows might have been bound for life by grief. "
      "Instead, back in London, Jane began spreading stories that Mary had been cold to her "
      "husband and had made his final years miserable. The whispers followed Mary for "
      "years. The friend who had seemed the most devoted had been the most envious all "
      "along.",
      mood="dark", ambience="sea", pause=1.0, beat="reveal", fallback="stormy sea"),
    S("s45", "whisper",
      "Envy is the emotion nobody admits to, because admitting it means admitting you feel "
      "inferior. So it hides, very often inside friendship, where comparison is sharpest. "
      "Watch for small signs. A flicker of disappointment when you share good news, or a "
      "flash of pleasure at your bad news. Praise with a hidden sting. Gossip behind your "
      "back. And a pattern of warmth and coldness that keeps you guessing. One sign means "
      "little. A pattern means a lot.",
      mood="tense",
      lower="Signs of envy, A flicker of joy · Poisoned praise · Gossip · Hot and cold",
      fallback="gossip"),
    S("s46", "forest landscape",
      "To protect yourself, don't flaunt your success. Mention your struggles, and your "
      "luck. To free yourself from your own envy, move closer to the people you envy, and "
      "you will see their lives hold pain too. Compare yourself with those who have less, "
      "and feel grateful. Practise sharing joy, actually feeling glad for others. Turn envy "
      "into emulation, a wish to rise rather than to pull someone down. And spend time with "
      "things so vast and beautiful that comparison simply falls away.",
      mood="hopeful", lower="Beyond envy, Gratitude · Shared joy · Emulation",
      fallback="landscape"),

    # Law 11
    S("s47", "Eisner Cuban",
      "Law eleven. The Law of Grandiosity. In nineteen eighty-four, Michael Eisner took over "
      "a tired, struggling Walt Disney Company. What followed was one of the great "
      "turnarounds in business history. With his partner Frank Wells beside him, and "
      "talented executives like Jeffrey Katzenberg, Disney returned to glory. Animated hits, "
      "booming theme parks, soaring profits. Eisner was hailed as a genius. And slowly, he "
      "came to believe it completely.",
      mood="driving", chapter="Law 11 · Know your limits|Grandiosity",
      lower="Michael Eisner, Head of Disney, 1984 to 2005", fallback="Walt Disney Studios", commons=True),
    S("s48", "Disneyland Paris",
      "In nineteen ninety-two, Euro Disney opened near Paris, and lost enormous sums in its "
      "early years. In ninety-four, Frank Wells died in a helicopter crash, and the steadying "
      "voice was gone. Katzenberg left after a bitter clash and helped found a rival studio. "
      "Eisner's hand-picked deputy lasted little more than a year. Shareholders revolted, and "
      "in two thousand and five, Eisner was gone. The success had been real. The belief "
      "that it was all his own was not.",
      mood="dark", pause=1.0, fallback="Disneyland castle"),
    S("s49", "lion roaring",
      "Greene calls this the success delusion. Every success involves luck, timing, and "
      "other people's help. But our minds quietly delete those parts, and keep the credit. "
      "As grandiosity grows, so do the warning signs. Overbearing certainty that plans will "
      "work. Extreme touchiness when criticised. Contempt for experts, and for anyone who "
      "says no. Big talk, and very little listening. Grandiose leaders even build myths "
      "around themselves. I was destined for this. I am one of you. I never fail.",
      mood="tense",
      lower="Signs of grandiosity, Certainty · Touchiness · Contempt for advice",
      fallback="statue"),
    S("s50", "mountain climber",
      "Yet Greene does not ask us to kill that energy. Wanting to be great is human, and it "
      "can drive real achievement. So channel it. Admit your need to feel important. Pour "
      "it into one focused project at a time. Keep a constant dialogue with reality by "
      "seeking honest feedback. After every success, list what luck and other people "
      "contributed. And choose challenges just a little above your current level. Tie your "
      "sense of greatness to your work, never to your image.",
      mood="hopeful",
      lower="Practical grandiosity, Focus · Feedback · Challenges just beyond you",
      fallback="climbing"),

    # Law 12
    S("s51", "Fiorentino Caterina Sforza Riario Berlin",
      "Law twelve. The Law of Gender Rigidity. Caterina Sforza was born in Renaissance Italy, "
      "the daughter of the Duke of Milan. Unusually for her time, she was raised much like "
      "the boys of the family. She learned to ride, to hunt, and to handle weapons, "
      "alongside poetry, art, and the graces of court. As an adult she ruled the cities of "
      "Imola and Forlì. She could charm ambassadors with elegance, and when enemies came, "
      "she could command soldiers herself.",
      mood="steady", chapter="Law 12 · Reclaim the masculine and feminine|Gender Rigidity",
      lower="Caterina Sforza, Countess of Forlì", fallback="Sforza", commons=True),
    S("s52", "Rocca di Ravaldino",
      "In fourteen eighty-eight, conspirators murdered her husband and seized Caterina and "
      "her children. She persuaded them to let her into the city's fortress, promising to "
      "talk the garrison into surrender. Once inside, she turned on them. When the plotters "
      "threatened her children beneath the walls, Caterina shouted back her defiance and "
      "refused to give in. Shaken, they lost their nerve, and her allies soon arrived. Her "
      "enemies had expected a frightened widow. They met a commander.",
      mood="driving", pause=1.0, beat="reveal", lower="The fortress of Ravaldino, 1488",
      fallback="Forli fortress"),
    S("s53", "yin yang",
      "Drawing on Carl Jung again, Greene argues that everyone is born with both masculine "
      "and feminine qualities. As we grow up, the pressure to fit an expected role makes us "
      "bury one side. But buried traits don't vanish. They return as projections. We fall "
      "for people who seem to carry the part of ourselves we have locked away, the bold "
      "rebel or the tender soul, and then feel betrayed when they turn out to be ordinary "
      "human beings.",
      mood="reflective", lower="Anima and animus, The hidden other half",
      fallback="yin and yang"),
    S("s54", "balanced stones",
      "The price of rigidity is a smaller range of responses. Someone who has suppressed "
      "empathy has fewer tools as a leader. Someone who has suppressed assertiveness "
      "struggles to stand up for themselves. So aim for balance. If you naturally analyse "
      "and break things apart, practise seeing the whole picture. If you charge into action, "
      "learn to step back and reflect. If you avoid conflict, get comfortable with it. The "
      "most powerful people play across the full range.",
      mood="hopeful",
      lower="Restore the balance, Analysis and intuition · Action and reflection",
      fallback="stone balance"),

    # Law 13
    S("s55", "Martin Luther King",
      "Law thirteen. The Law of Aimlessness. In nineteen fifty-five, a twenty-six-year-old "
      "pastor in Montgomery, Alabama, was suddenly asked to lead a boycott of the city's "
      "segregated buses. Martin Luther King Junior had not planned to become the face of a "
      "movement. Soon the threats began. Phone calls at all hours, promising to kill him and "
      "bomb his home. Late one night, after yet another call, he sat at his kitchen table, "
      "afraid, and ready to give up.",
      mood="tense", chapter="Law 13 · Advance with a sense of purpose|Aimlessness",
      lower="Martin Luther King Jr., Montgomery, Alabama, 1955", fallback="King"),
    S("s56", "Montgomery bus boycott",
      "In that moment of despair, King later said, he felt an inner voice urging him to "
      "stand up for justice, and to keep going. His fear eased. From then on, his purpose "
      "came from inside him, not from a role others had handed him. Days later his house "
      "was bombed, and he calmed the angry crowd outside. The boycott lasted more than a "
      "year, and it won. That inner sense of calling carried him through more than a "
      "decade of danger.",
      mood="hopeful", pause=1.0, beat="reveal", fallback="civil rights march"),
    S("s57", "compass",
      "Greene argues that without a real purpose, we drift. Pulled by moods, by other "
      "people's opinions, by whatever pays the bills. To fill the emptiness, we grab at "
      "false purposes. The chase for pleasure. Causes we join mainly to belong. Money and "
      "fame for their own sake. The hunger for attention. Or cynicism, the pose of believing "
      "in nothing. Each gives a brief lift, and then leaves us emptier than before.",
      mood="reflective",
      lower="False purposes, Pleasure · Causes · Money · Attention · Cynicism",
      fallback="old compass"),
    S("s58", "craftsman workshop",
      "To find a real purpose, look back to childhood, to the subjects and activities that "
      "fascinated you before anyone told you what to like. That is the voice of your "
      "uniqueness. Use setbacks and resistance as fuel. Spend time with people who have a "
      "strong sense of purpose, because that energy is contagious. Build a ladder of goals, "
      "from the big mission down to what you will do this month. Then lose yourself in the "
      "work. Purpose multiplies the force of everything you do.",
      mood="hopeful",
      lower="Find your purpose, Childhood clues · Mentors · A ladder of goals · Deep work",
      fallback="carpenter"),

    # Law 14
    S("s59", "Cultural Revolution propaganda poster featuring Mao Zedong",
      "Law fourteen. The Law of Conformity. In nineteen sixty-six, Gao Yuan was a teenage "
      "student in a small Chinese city when Chairman Mao launched the Cultural Revolution. "
      "Students were told to root out enemies of the revolution, even among their own "
      "teachers. At first it felt thrilling, idealistic, a chance to build a pure new "
      "society. Gao and his classmates joined in, wearing red armbands and chanting "
      "slogans.",
      mood="dark", chapter="Law 14 · Resist the downward pull of the group|Conformity",
      lower="Gao Yuan, A schoolboy in Mao's China, 1966", fallback="Mao Zedong", commons=True),
    S("s60", "Mao Zedong and the Red Guards cut paper",
      "Within months, his school had split into rival factions, each claiming to be the true "
      "revolutionaries. Teachers were humiliated in public. Friends betrayed friends to "
      "prove their loyalty. The rival groups fought with clubs, and later with guns. "
      "Ordinary teenagers did things they could never have imagined doing alone. Mao had "
      "tried to remake human nature through fervour. Instead, he exposed it. Status games, "
      "tribes, and scapegoats, the same as in any group.",
      mood="dark", pause=1.0, lower="The Red Guards, 1966 to 1968",
      fallback="Cultural Revolution poster", commons=True),
    S("s61", "crowd",
      "Greene's warning is that we all have a social self, a different person who appears in "
      "groups. In a crowd we want to fit in, so we copy what others think. We perform our "
      "loyalty. We catch the group's emotions, especially fear and anger, which spread "
      "fastest. And we feel strangely certain, because everyone around us agrees. Whenever "
      "you feel unusually excited and sure about an idea that everyone shares, step back, "
      "and ask whether you are thinking, or just conforming.",
      kind="video", mood="tense", ambience="crowd",
      lower="The group effect, Fit in · Perform · Catch the mood · Feel certain",
      fallback="people crowd"),
    S("s62", "team meeting",
      "Every group with a leader grows a court, full of characters. The intriguer. The "
      "gatekeeper who controls access. The flatterer. The jester. And the punching bag "
      "everyone picks on. Learn to recognise them, and be kind to the punching bag. As a "
      "leader, build what Greene calls a reality group. Give it a clear shared purpose. "
      "Choose capable lieutenants, not friends. Let information flow freely, including "
      "criticism. And keep everyone focused on results rather than politics.",
      mood="steady",
      lower="The reality group, Shared purpose · Open information · Results first",
      fallback="meeting"),

    # Law 15
    S("s63", "Elizabeth I in coronation robes",
      "Law fifteen. The Law of Fickleness. When Elizabeth the First took the English throne "
      "in fifteen fifty-eight, almost nobody expected her to last. She was twenty-five, "
      "unmarried, and a woman in a world that assumed only men could truly rule. The country "
      "was split over religion, nearly bankrupt, and surrounded by stronger enemies. Her own "
      "council assumed she would marry quickly and let a husband govern. They badly "
      "underestimated her.",
      mood="steady", chapter="Law 15 · Make them want to follow you|Fickleness",
      lower="Elizabeth I, Queen of England, 1558 to 1603", fallback="Queen Elizabeth", commons=True),
    S("s64", "Spanish Armada fireships",
      "Elizabeth never assumed her authority was secure. She worked tirelessly, kept her "
      "council's trust, and spent carefully, sparing her people crushing taxes. She toured "
      "the country so ordinary subjects could see her. She showed patience with threats, "
      "like her cousin Mary, Queen of Scots, until action became unavoidable. And when the "
      "Spanish Armada came in fifteen eighty-eight, she rode out to speak to her troops. She "
      "reigned for forty-four years.",
      mood="epic", ambience="sea", beat="reveal", lower="The Spanish Armada, 1588",
      fallback="Armada", commons=True),
    S("s65", "crown jewels",
      "Greene's law is that people are always ambivalent about their leaders. They want to "
      "be led, yet they resent being told what to do. Leaders often fall into what Greene "
      "calls the entitlement curse, the belief that a title brings automatic loyalty. It "
      "does not. Authority must be earned, and then earned again. The moment you seem weak, "
      "selfish, or petty, followers begin to drift. People follow those who seem to serve "
      "something larger than themselves.",
      mood="steady", lower="The entitlement curse, A title is not authority",
      fallback="royal crown"),
    S("s66", "lighthouse",
      "So build real authority. Find a leadership style that fits your natural character, "
      "instead of copying someone else's. Focus on the group's needs, and listen. Develop a "
      "vision, then work backward to practical steps. Lead from the front, working harder "
      "and sacrificing first. Balance presence and absence, because a leader who talks too "
      "much loses their aura. Never seem to take, only to give. And above all, master "
      "yourself. Inner authority is what followers feel.",
      mood="hopeful",
      lower="Real authority, Vision · Lead from the front · Presence and absence",
      fallback="lighthouse sea"),

    # Law 16
    S("s67", "John Davison Rockefeller 1839-1937 LCCN2004679693",
      "Law sixteen. The Law of Aggression. John D. Rockefeller seemed like the gentlest man "
      "imaginable. Soft-spoken, devout, never raising his voice. A quiet bookkeeper who gave "
      "generously to his church. Yet in the chaotic oil business of the eighteen sixties "
      "and seventies, he became one of the most ruthless competitors in history. Behind the "
      "calm, pious surface burned an iron will to control everything.",
      mood="tense", chapter="Law 16 · See the hostility behind the facade|Aggression",
      lower="John D. Rockefeller, Founder of Standard Oil", fallback="Rockefeller", commons=True),
    S("s68", "Rockefeller Monument derrick",
      "Rockefeller struck secret deals with the railroads for cheap shipping his rivals "
      "could not match. Then he went to competing refiners with a simple offer. Sell to me, "
      "or be crushed. One by one, they sold. Within a decade, Standard Oil controlled about "
      "nine-tenths of American oil refining. And Rockefeller sincerely believed he was "
      "doing good, bringing order to a wasteful, chaotic industry. The most dangerous "
      "aggressors always have a noble story.",
      mood="dark", pause=1.0, beat="reveal", lower="Standard Oil, Cleveland, 1870",
      fallback="oil refinery", commons=True),
    S("s69", "chess king",
      "Greene argues that aggression is part of human nature, and that it hides behind "
      "friendly and even moral faces. Chronic aggressors leave a trail of enemies, and "
      "always have a lofty explanation. More common still is passive aggression. The "
      "colleague who is always late, to make you wait. The person who plays the victim to "
      "win sympathy. The one who promises and never delivers. The one who shifts every bit "
      "of blame. Stay calm. They feed on your reaction.",
      mood="tense",
      lower="Passive aggression, Superiority · Sympathy · Dependency · Blame",
      fallback="chess pieces"),
    S("s70", "boxing training",
      "Against open aggressors, don't fight head-on. Outthink them. Find their weak points, "
      "expose their hypocrisy, and make a conflict with you more costly than they expected. "
      "And turn the law inward. Your own aggressive energy is not evil. Channel it. Revive "
      "the ambitions of your youth, and make them specific. Be relentlessly persistent. "
      "Train yourself to be fearless. And aim your anger precisely, at real problems, not at "
      "the world in general.",
      mood="driving",
      lower="Controlled aggression, Ambition · Persistence · Fearlessness · Aimed anger",
      fallback="boxer"),

    # Law 17
    S("s71", "Georges Danton",
      "Law seventeen. The Law of Generational Myopia. Georges Danton was a big, "
      "rough-voiced, ambitious young lawyer in Paris in the years before seventeen "
      "eighty-nine. He sensed something many older people missed. A new generation had "
      "grown up on new ideas, liberty, equality, the rights of man, and it was losing all "
      "patience with the old order. Danton felt that rising mood in the cafés and the "
      "streets, and he gave it a voice.",
      mood="driving", chapter="Law 17 · Seize the historical moment|Generational Myopia",
      lower="Georges Danton, Revolutionary", fallback="Danton"),
    S("s72", "Execution of Louis XVI",
      "King Louis the Sixteenth, by contrast, was trapped in the mindset of his upbringing. "
      "He assumed the old rules of royal authority still held, that his subjects would "
      "always revere their king. He could not feel the tide turning until it swept him "
      "away, and he died on the guillotine. Yet Danton's story turns too. When he called for "
      "an end to the bloody Terror, he moved against the mood of the moment, and in "
      "seventeen ninety-four, the same tide carried him to the guillotine as well.",
      mood="dark", pause=1.0, beat="reveal", lower="Louis XVI, King of France",
      fallback="French Revolution", commons=True),
    S("s73", "family generations",
      "Greene argues that your generation shapes you far more than you realise. The events of "
      "your youth, the crises or the calm, the heroes and the music, set a lens you carry for "
      "life. And history moves in patterns. A revolutionary generation breaks with the past. "
      "The next one craves order. The next becomes practical and comfortable. The next grows "
      "restless, and questions everything. Each tends to react against the one before it.",
      mood="reflective",
      lower="Generational patterns, Each generation pushes against the last",
      fallback="old photograph family"),
    S("s74", "old clock",
      "So become generationally aware. Study the defining events of your own generation, "
      "and the frustrations hiding behind its humour. Sense the mood of the rising one, "
      "before it becomes obvious. Then work with the spirit of the times, not against it. "
      "Redirect the current gently instead of fighting it. Keep friendships across the "
      "generations, and keep adapting. The person who can feel where the tide is heading "
      "can ride it, instead of being drowned by it.",
      mood="hopeful",
      lower="Seize the moment, Read the spirit · Adapt · Mix with every age",
      fallback="clock"),

    # Law 18
    S("s75", "peacock",
      "Law eighteen, the final law. The Law of Death Denial. In nineteen fifty, a gifted "
      "young writer named Flannery O'Connor fell seriously ill. She was told it was "
      "rheumatoid arthritis. In fact, it was lupus, the disease that had killed her father. "
      "When she learned the truth, she understood she might have only a few years left. She "
      "moved back to her mother's farm in Georgia, raised peacocks, and gave herself "
      "completely to her writing.",
      mood="reflective", chapter="Law 18 · Meditate on our common mortality|Death Denial",
      lower="Flannery O'Connor, Writer", fallback="peacock feathers"),
    S("s76", "farmhouse sunset",
      "Death did not make her bitter. It made her focused. Knowing her time was short, she "
      "wrote with fierce urgency, producing stories now counted among the finest in "
      "American literature. Her illness also sharpened her eye for people, their pride, "
      "their frailty, their need for grace. She lived about thirteen more years, and she "
      "made every one of them count. She died at thirty-nine.",
      mood="reflective", pause=1.5, beat="reveal", fallback="farm"),
    S("s77", "candle",
      "Greene argues that most of us organise our lives around avoiding the thought of "
      "death, and the denial costs us. We waste time as if we had an endless supply. We blow "
      "small grievances out of proportion. We feel separate from other people. But those "
      "who face mortality directly often describe a strange effect. They feel more awake, "
      "more alive, more grateful. Awareness of the end gives everything before it its "
      "weight.",
      mood="reflective", lower="The death effect, Facing death makes us more alive",
      fallback="candle flame"),
    S("s78", "night sky stars",
      "So make that awareness real. Imagine, vividly, that your time is limited, because it "
      "is. Let that urgency clarify what really matters today. Look at the strangers on a "
      "busy street and remember that within a century, all of us will be gone. That shared "
      "fate connects us. Embrace even pain and setbacks as part of a life you accept "
      "completely, what Nietzsche called the love of fate. And look up at the night sky now "
      "and then. Feel how small, and how lucky, you are.",
      mood="reflective", ambience="night",
      lower="Live with death in view, Urgency · Shared fate · Love of fate · Awe",
      fallback="milky way"),

    # Closing
    S("s79", "sunrise city",
      "So there they are. Eighteen laws, from irrationality to our own mortality. Taken "
      "together, they say something both humbling and hopeful. We are not as rational, "
      "independent, or good as we like to believe. But we are not trapped either. Every one "
      "of these forces can be noticed. And whatever you can notice, you can begin to master. "
      "Greene's real aim is not to make us manipulators. It is to make us calmer, wiser, and "
      "more deeply empathetic.",
      mood="hopeful", chapter="Final thoughts|Beyond the Mask", fallback="city skyline"),
    S("s80", "people walking sunset",
      "The next time someone surprises you with envy, anger, or sudden kindness, pause. Look "
      "beneath the mask. Then turn the same curious eye on yourself. That is the work of a "
      "lifetime, and it starts today. If this summary was useful, pick up Robert Greene's "
      "book. It is full of stories and depth we could only touch on here. Thank you for "
      "listening.",
      kind="video", beat="outro", mood="reflective", transition="dip", fallback="sunset"),
]


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def shot_seconds(scene: S) -> float:
    return round(max(8.0, min(30.0, _words(scene.narration) / SPEAKING_WPM * 60.0 + 2.5)), 1)


#: The film is split where Law 10 begins, so each part stays under the
#: app's 30-minute limit. Part one is laws 1 to 9; part two is laws 10 to 18.
PART_BREAK = "s43"
PARTS = {1: SCENES[:[s.key for s in SCENES].index(PART_BREAK)],
         2: SCENES[[s.key for s in SCENES].index(PART_BREAK):]}


def build_spec(picks: dict[str, dict], part: int = 1) -> dict:
    scenes = []
    for scene in PARTS[part]:
        pick = picks[scene.key]
        entry = {
            "scene_key": scene.key,
            "purpose": scene.lower or scene.query,
            "target_duration_s": shot_seconds(scene),
            "narration": scene.narration,
            "dialogue": [],
            "characters": [],
            "image_hint": pick["still"],
            "camera": "",
            "transition": scene.transition,
            "preferred_method": "camera_move",
            "beat": scene.beat,
            "music_mood": scene.mood,
            "ambience": scene.ambience,
        }
        if scene.pause is not None:
            entry["pause_after_s"] = scene.pause
        if scene.chapter:
            entry["chapter_title"] = scene.chapter
        if scene.lower:
            entry["lower_third"] = scene.lower
        if pick.get("footage"):
            entry["footage"] = {
                "path": pick["footage"],
                "credit": pick["credit"],
                "licence": pick["licence"],
                "start_s": pick.get("excerpt_start_s"),
            }
        scenes.append(entry)
    total = round(sum(s["target_duration_s"] for s in scenes))
    return {
        "name": f"The Laws of Human Nature, Part {part} of 2",
        "niche": "book summary",
        "story_style": "documentary",
        "audience": "general",
        "brief": "A complete, original-words summary of Robert Greene's The Laws of Human "
                 "Nature: all eighteen laws, the story behind each, and how to use them.",
        "stills_folder": str(STILLS),
        "stills_ai_generated": False,
        "voice": "kokoro:am_michael",
        "music_path": None,
        "music_gain_db": -30,
        "target_duration_s": total,
        "resolution": "1080p",
        "aspect_ratio": "16:9",
        "fps": 24,
        "finishing": {
            "look": "clean",
            "end_card": "Part two next" if part == 1 else "Thanks for listening",
            "title_card": "",
            "ambience_gain_db": -34,
            "effects_gain_db": -22,
        },
        "title": f"The Laws of Human Nature, Part {part} of 2",
        "logline": (
            "Laws one to nine of Robert Greene's laws of human nature, the stories behind them, "
            "and how to use them." if part == 1 else
            "Laws ten to eighteen of Robert Greene's laws of human nature, the stories behind "
            "them, and how to use them."
        ),
        "synopsis": "From Pericles and the irrational Athenians to Flannery O'Connor facing "
                    "death: narcissism, masks, character, desire, envy, grandiosity, the "
                    "group, leadership, aggression, generations and mortality.",
        "format": "documentary",
        "scenes": scenes,
    }


def check() -> None:
    keys = [s.key for s in SCENES]
    if len(keys) != len(set(keys)):
        raise SystemExit("Duplicate scene keys.")
    for scene in SCENES:
        if scene.chapter and len(scene.chapter) > MAX_CHAPTER:
            raise SystemExit(f"{scene.key}: chapter title is {len(scene.chapter)} characters.")
        if scene.lower and len(scene.lower) > MAX_LOWER:
            raise SystemExit(f"{scene.key}: label is {len(scene.lower)} characters.")


async def fetch_all(only: set[str] | None = None) -> dict[str, dict]:
    status = await ffmpeg.detect()
    if not status.ffmpeg_path:
        raise SystemExit("FFmpeg is needed to cut footage.")
    STILLS.mkdir(parents=True, exist_ok=True)
    FILM_DIR.mkdir(parents=True, exist_ok=True)
    picks: dict[str, dict] = json.loads(PICKS.read_text(encoding="utf-8")) if PICKS.exists() else {}
    wanted = [s for s in SCENES if (s.key in only if only else s.key not in picks)]
    used: set[str] = {v["page"] for k, v in picks.items() if k not in {s.key for s in wanted}}
    for scene in wanted:
        for old in STILLS.glob(f"{scene.key}-*"):
            old.unlink()
        kinds = [scene.kind, "image"] if scene.kind == "video" else ["image"]
        chosen = None
        for kind in kinds:
            for query in (scene.query, scene.fallback):
                try:
                    if scene.commons:
                        found = [c for c in await stock_media.search_commons(query, kind)
                                 if stock_media.usable(c, query)]
                        found.sort(key=lambda c: c.score, reverse=True)
                    else:
                        found = await stock_media.search(query, kind)
                except stock_media.StockMediaError as exc:
                    print(scene.key, kind, "search failed:", exc, flush=True)
                    continue
                for candidate in [c for c in found if c.page_url not in used][:10]:
                    try:
                        fetched = await stock_media.fetch(
                            candidate, STILLS, ffmpeg_path=status.ffmpeg_path, name=scene.key
                        )
                    except stock_media.StockMediaError as exc:
                        print(scene.key, "skipped:", exc, flush=True)
                        continue
                    chosen = (candidate, fetched)
                    break
                if chosen:
                    break
            if chosen:
                break
        if chosen is None:
            print(scene.key, "NO MEDIA for", scene.query, flush=True)
            continue
        candidate, fetched = chosen
        used.add(candidate.page_url)
        picks[scene.key] = {
            "query": scene.query,
            "kind": candidate.kind,
            "still": fetched.still_path.name,
            "footage": str(fetched.footage_path) if fetched.footage_path else None,
            "page": candidate.page_url,
            "licence": candidate.licence,
            "credit": candidate.credit_line,
            "title": candidate.title,
            "source_duration_s": candidate.duration_s,
            "excerpt_start_s": candidate.extra.get("excerpt_start_s"),
        }
        # Saved after every shot, so a dropped connection loses nothing.
        PICKS.write_text(json.dumps(picks, indent=2), encoding="utf-8")
        print(scene.key, candidate.kind, candidate.licence, "->", fetched.still_path.name,
              "|", candidate.title[:60], flush=True)
    return picks


def contact_sheet(picks: dict[str, dict]) -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    FILM_DIR.mkdir(parents=True, exist_ok=True)
    cell_w, cell_h, cols, per_sheet = 384, 216, 4, 16
    font = ImageFont.load_default()
    sheets: list[Path] = []
    keys = [s.key for s in SCENES if s.key in picks]
    for start in range(0, len(keys), per_sheet):
        batch = keys[start:start + per_sheet]
        rows = (len(batch) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + 22)), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for index, key in enumerate(batch):
            x, y = (index % cols) * cell_w, (index // cols) * (cell_h + 22)
            image = Image.open(STILLS / picks[key]["still"]).convert("RGB")
            image.thumbnail((cell_w - 6, cell_h))
            sheet.paste(image, (x + 3, y + 22))
            label = f"{key}  {picks[key]['kind']}  {picks[key]['query'][:28]}"
            draw.text((x + 6, y + 5), label, fill=(235, 235, 235), font=font)
        path = SHEET.with_name(f"{SHEET.name}-{start // per_sheet + 1}.jpg")
        sheet.save(path, quality=90)
        sheets.append(path)
    return sheets


def main() -> None:
    check()
    command = sys.argv[1] if len(sys.argv) > 1 else "spec"
    if command == "fetch":
        only = set(sys.argv[2:]) or None
        asyncio.run(fetch_all(only))
        return
    if command == "sheet":
        picks = json.loads(PICKS.read_text(encoding="utf-8"))
        for path in contact_sheet(picks):
            print(path)
        return
    if command == "spec":
        picks = json.loads(PICKS.read_text(encoding="utf-8"))
        missing = [s.key for s in SCENES if s.key not in picks]
        if missing:
            raise SystemExit(f"Missing picks for {missing}. Run fetch first.")
        FILM_DIR.mkdir(parents=True, exist_ok=True)
        for part in (1, 2):
            spec = build_spec(picks, part)
            path = FILM_DIR / f"spec-part{part}.json"
            path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
            words = sum(_words(s.narration) for s in PARTS[part])
            print(f"part {part}: {len(PARTS[part])} shots, {words} words, "
                  f"planned {spec['target_duration_s']}s -> {path}")
        return
    if command == "words":
        words = sum(_words(s.narration) for s in SCENES)
        print(f"{len(SCENES)} shots, {words} words, about {words / SPEAKING_WPM:.1f} minutes")
        return
    raise SystemExit(f"Unknown command {command!r}.")


if __name__ == "__main__":
    main()
