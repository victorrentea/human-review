package ro.victorrentea.humanreview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

// `jetbrains://idea/human-review/<target>?…`: the same query parameters as uri-handler.js, and
// the same routing — the deepest folder wins, across this IDE's projects and the other bridges.
class LinksTest {
    @Test fun `open carries file, line and the end of a range`() {
        assertEquals(LinkRequest.Open("/r/A.java", 49, "51"),
            Links.parse("open", mapOf("file" to "/r/A.java", "line" to "49", "endLine" to "51")))
        assertEquals(LinkRequest.Open("/r/A.java", 1, null), Links.parse("open", mapOf("file" to "/r/A.java")))
        assertEquals(LinkRequest.Open("/r/A.java", 1, null),
            Links.parse("open", mapOf("file" to "/r/A.java", "line" to "x", "endLine" to "")))
        assertEquals(LinkRequest.Open("/r/A.java", 1, null), Links.parse("open", mapOf("file" to "/r/A.java", "line" to "-3")))
    }

    @Test fun `diff carries the base`() {
        assertEquals(LinkRequest.Diff("/r/My File.java", "origin/main", 12),
            Links.parse("diff", mapOf("file" to "/r/My File.java", "base" to "origin/main", "line" to "12")))
    }

    @Test fun `a commit's diff carries its head`() {
        assertEquals(LinkRequest.Diff("/r/A.java", "abc1234", 380, "def5678"),
            Links.parse("diff", mapOf("file" to "/r/A.java", "base" to "abc1234", "head" to "def5678", "line" to "380")))
        assertEquals(null, (Links.parse("diff", mapOf("file" to "/r/A.java", "base" to "x", "head" to "")) as LinkRequest.Diff).head)
    }

    @Test fun `review-open carries the commit stamp`() {
        val got = Links.parse("review-open", mapOf("file" to "/r/A.java", "line" to "3", "sha" to "abc1234",
            "root" to "/r", "branch" to "test-pr")) as LinkRequest.ReviewOpen
        assertEquals(ReviewRef("/r/A.java", "3", null, "abc1234", "/r", "test-pr"), got.ref)
    }

    @Test fun `anything else is unknown, named`() {
        assertEquals(LinkRequest.Unknown("reload"), Links.parse("reload", emptyMap()))
        assertEquals(LinkRequest.Unknown(""), Links.parse(null, emptyMap()))
    }

    private fun f(path: String) = Folder(path.substringAfterLast('/'), path, path)

    @Test fun `the deepest folder wins across this IDE and the others`() {
        val here = listOf("ij-workspace" to f("/w"), "ij-shop" to f("/w/shop"))
        val there = listOf("vsc-shop-clone" to f("/w/shop-clone"), "vsc-deep" to f("/w/shop/module"))
        assertEquals("ij-shop", Links.owner("/w/shop/src/A.java", here, there))
        assertEquals("vsc-deep", Links.owner("/w/shop/module/B.java", here, there))
        assertEquals("vsc-shop-clone", Links.owner("/w/shop-clone/A.java", here, there))
        assertEquals("ij-workspace", Links.owner("/w/notes.md", here, there))
        assertNull(Links.owner("/elsewhere/A.java", here, there))
    }

    @Test fun `an equal claim stays here`() {
        assertEquals("here", Links.owner("/w/shop/A.java", listOf("here" to f("/w/shop")), listOf("there" to f("/w/shop"))))
    }
}
